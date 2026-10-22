// SPDX-License-Identifier: GPL-2.0-only OR BSD-3-Clause
/*
 * Copyright (c) 2019 Isaac Lozano <109lozanoi@gmail.com>
 *
 * This code is released using a dual license strategy: BSD/GPL
 * You can choose the licence that better fits your requirements.
 *
 * Released under the terms of 3-clause BSD License
 * Released under the terms of GNU General Public License Version 2.0
 *
 */

#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/slab.h>
#include <linux/usb.h>
#include <sound/core.h>
#include <sound/initval.h>
#include <sound/pcm.h>

#include "gvusb2.h"

#define CARD_NAME "GV-USB2"

static int index = SNDRV_DEFAULT_IDX1;
static char *id = SNDRV_DEFAULT_STR1;

module_param(index, int, 0444);
MODULE_PARM_DESC(index, "Index value for the " CARD_NAME " soundcard.");
module_param(id, charp, 0444);
MODULE_PARM_DESC(id, "ID string for the " CARD_NAME " soundcard.");

struct gvusb2_snd {
	struct gvusb2_dev gv;
	struct usb_interface *intf;
	struct snd_card *card;
	int packet_size;

	struct mutex urb_lock;
	struct urb *urbs[GVUSB2_SND_URBS];
	bool urbs_running;
	bool disconnected;

	spinlock_t lock;
	struct snd_pcm_substream *substream;
	bool capturing;
	unsigned int dma_pos;
	unsigned int period_pos;
};

static const struct snd_pcm_hardware gvusb2_snd_hw = {
	.info = (SNDRV_PCM_INFO_MMAP |
		SNDRV_PCM_INFO_INTERLEAVED |
		SNDRV_PCM_INFO_BLOCK_TRANSFER |
		SNDRV_PCM_INFO_MMAP_VALID),
	.formats = SNDRV_PCM_FMTBIT_S16_LE,
	.rates = SNDRV_PCM_RATE_48000,
	.rate_min = 48000,
	.rate_max = 48000,
	.channels_min = 2,
	.channels_max = 2,
	.buffer_bytes_max = 128 * 1024,
	.period_bytes_min = 768,
	.period_bytes_max = 64 * 1024,
	.periods_min = 2,
	.periods_max = 1024,
};

static void gvusb2_snd_kill_urbs(struct gvusb2_snd *dev)
{
	int i;

	mutex_lock(&dev->urb_lock);
	for (i = 0; i < GVUSB2_SND_URBS; i++)
		usb_kill_urb(dev->urbs[i]);
	dev->urbs_running = false;
	mutex_unlock(&dev->urb_lock);
}

static int gvusb2_snd_start_urbs(struct gvusb2_snd *dev)
{
	int i, ret = 0;

	mutex_lock(&dev->urb_lock);
	if (dev->disconnected) {
		ret = -ENODEV;
	} else if (!dev->urbs_running) {
		for (i = 0; i < GVUSB2_SND_URBS; i++) {
			ret = usb_submit_urb(dev->urbs[i], GFP_KERNEL);
			if (ret < 0) {
				dev_err(&dev->intf->dev,
					"error submitting audio urb (%d)\n", ret);
				while (--i >= 0)
					usb_kill_urb(dev->urbs[i]);
				break;
			}
		}
		dev->urbs_running = ret == 0;
	}
	mutex_unlock(&dev->urb_lock);

	return ret;
}

static void gvusb2_snd_free_urbs(struct gvusb2_snd *dev)
{
	int i;

	mutex_lock(&dev->urb_lock);
	for (i = 0; i < GVUSB2_SND_URBS; i++) {
		struct urb *urb = dev->urbs[i];

		if (urb != NULL) {
			usb_kill_urb(urb);
			kfree(urb->transfer_buffer);
			usb_free_urb(urb);
			dev->urbs[i] = NULL;
		}
	}
	dev->urbs_running = false;
	mutex_unlock(&dev->urb_lock);
}

static void gvusb2_snd_process_isoc(struct gvusb2_snd *dev, struct urb *urb)
{
	struct snd_pcm_substream *substream;
	struct snd_pcm_runtime *runtime;
	unsigned int period_bytes;
	bool elapsed = false;
	unsigned long flags;
	int i;

	spin_lock_irqsave(&dev->lock, flags);

	substream = dev->substream;
	if (!dev->capturing || substream == NULL) {
		spin_unlock_irqrestore(&dev->lock, flags);
		return;
	}
	runtime = substream->runtime;
	period_bytes = frames_to_bytes(runtime, runtime->period_size);

	for (i = 0; i < urb->number_of_packets; i++) {
		u8 *buf = urb->transfer_buffer + urb->iso_frame_desc[i].offset;
		unsigned int len = urb->iso_frame_desc[i].actual_length;

		if (urb->iso_frame_desc[i].status < 0 || len == 0)
			continue;

		while (len > 0) {
			unsigned int n = min(len, (unsigned int)runtime->dma_bytes -
				dev->dma_pos);

			memcpy(runtime->dma_area + dev->dma_pos, buf, n);
			dev->dma_pos += n;
			if (dev->dma_pos >= runtime->dma_bytes)
				dev->dma_pos = 0;
			buf += n;
			len -= n;

			dev->period_pos += n;
			if (dev->period_pos >= period_bytes) {
				dev->period_pos -= period_bytes;
				elapsed = true;
			}
		}
	}

	spin_unlock_irqrestore(&dev->lock, flags);

	if (elapsed)
		snd_pcm_period_elapsed(substream);
}

static void gvusb2_snd_isoc_irq(struct urb *urb)
{
	int i, ret;
	struct gvusb2_snd *dev = urb->context;

	switch (urb->status) {
	case 0:
	case -EXDEV:
		break;
	case -ECONNRESET:
	case -ENOENT:
	case -ESHUTDOWN:
	case -ENODEV:
		return;
	default:
		gvusb2_dbg(&dev->intf->dev, "audio urb error %d\n", urb->status);
		goto resubmit;
	}

	gvusb2_snd_process_isoc(dev, urb);

resubmit:
	for (i = 0; i < urb->number_of_packets; i++) {
		urb->iso_frame_desc[i].status = 0;
		urb->iso_frame_desc[i].actual_length = 0;
	}

	ret = usb_submit_urb(urb, GFP_ATOMIC);
	if (ret && ret != -EPERM && ret != -ENODEV)
		dev_err_ratelimited(&dev->intf->dev,
			"audio urb resubmit failed (%d)\n", ret);
}

static int gvusb2_snd_allocate_urbs(struct gvusb2_snd *dev)
{
	int i;
	int size = GVUSB2_SND_PACKETS * dev->packet_size;

	for (i = 0; i < GVUSB2_SND_URBS; i++) {
		struct urb *urb;
		int pidx;

		urb = usb_alloc_urb(GVUSB2_SND_PACKETS, GFP_KERNEL);
		if (urb == NULL)
			goto nomem;

		dev->urbs[i] = urb;

		urb->transfer_buffer = kzalloc(size, GFP_KERNEL);
		if (urb->transfer_buffer == NULL)
			goto nomem;

		urb->dev = dev->gv.udev;
		urb->pipe = usb_rcvisocpipe(dev->gv.udev, GVUSB2_AUDIO_EP);
		urb->transfer_buffer_length = size;
		urb->complete = gvusb2_snd_isoc_irq;
		urb->context = dev;
		urb->interval = 1;
		urb->start_frame = 0;
		urb->number_of_packets = GVUSB2_SND_PACKETS;
		urb->transfer_flags = URB_ISO_ASAP;

		for (pidx = 0; pidx < GVUSB2_SND_PACKETS; pidx++) {
			urb->iso_frame_desc[pidx].offset = pidx * dev->packet_size;
			urb->iso_frame_desc[pidx].length = dev->packet_size;
		}
	}

	return 0;

nomem:
	gvusb2_snd_free_urbs(dev);
	return -ENOMEM;
}

static int gvusb2_snd_capture_open(struct snd_pcm_substream *substream)
{
	struct gvusb2_snd *dev = snd_pcm_substream_chip(substream);
	unsigned long flags;
	int ret = 0;

	spin_lock_irqsave(&dev->lock, flags);
	if (dev->substream == NULL) {
		dev->substream = substream;
		dev->capturing = false;
	} else {
		ret = -EBUSY;
	}
	spin_unlock_irqrestore(&dev->lock, flags);

	if (ret == 0)
		substream->runtime->hw = gvusb2_snd_hw;

	return ret;
}

static int gvusb2_snd_capture_close(struct snd_pcm_substream *substream)
{
	struct gvusb2_snd *dev = snd_pcm_substream_chip(substream);
	unsigned long flags;

	gvusb2_snd_kill_urbs(dev);

	spin_lock_irqsave(&dev->lock, flags);
	dev->capturing = false;
	dev->substream = NULL;
	spin_unlock_irqrestore(&dev->lock, flags);

	return 0;
}

static int gvusb2_snd_hw_free(struct snd_pcm_substream *substream)
{
	struct gvusb2_snd *dev = snd_pcm_substream_chip(substream);

	gvusb2_snd_kill_urbs(dev);

	return 0;
}

static int gvusb2_snd_pcm_prepare(struct snd_pcm_substream *substream)
{
	struct gvusb2_snd *dev = snd_pcm_substream_chip(substream);
	unsigned long flags;

	spin_lock_irqsave(&dev->lock, flags);
	dev->capturing = false;
	dev->dma_pos = 0;
	dev->period_pos = 0;
	spin_unlock_irqrestore(&dev->lock, flags);

	return gvusb2_snd_start_urbs(dev);
}

static int gvusb2_snd_pcm_trigger(struct snd_pcm_substream *substream, int cmd)
{
	struct gvusb2_snd *dev = snd_pcm_substream_chip(substream);

	switch (cmd) {
	case SNDRV_PCM_TRIGGER_START:
	case SNDRV_PCM_TRIGGER_RESUME:
		spin_lock(&dev->lock);
		dev->capturing = true;
		spin_unlock(&dev->lock);
		return 0;
	case SNDRV_PCM_TRIGGER_STOP:
	case SNDRV_PCM_TRIGGER_SUSPEND:
		spin_lock(&dev->lock);
		dev->capturing = false;
		spin_unlock(&dev->lock);
		return 0;
	default:
		return -EINVAL;
	}
}

static snd_pcm_uframes_t gvusb2_snd_pcm_pointer(
	struct snd_pcm_substream *substream)
{
	struct gvusb2_snd *dev = snd_pcm_substream_chip(substream);
	snd_pcm_uframes_t pos;

	spin_lock(&dev->lock);
	pos = bytes_to_frames(substream->runtime, dev->dma_pos);
	spin_unlock(&dev->lock);

	return pos;
}

static const struct snd_pcm_ops gvusb2_snd_capture_ops = {
	.open      = gvusb2_snd_capture_open,
	.close     = gvusb2_snd_capture_close,
	.hw_free   = gvusb2_snd_hw_free,
	.prepare   = gvusb2_snd_pcm_prepare,
	.trigger   = gvusb2_snd_pcm_trigger,
	.pointer   = gvusb2_snd_pcm_pointer,
};

static int gvusb2_snd_find_altsetting(struct usb_interface *intf,
	int *alt, int *packet_size)
{
	int i, ep;

	*alt = -1;
	*packet_size = 0;

	for (i = 0; i < intf->num_altsetting; i++) {
		struct usb_host_interface *as = &intf->altsetting[i];

		for (ep = 0; ep < as->desc.bNumEndpoints; ep++) {
			struct usb_endpoint_descriptor *e = &as->endpoint[ep].desc;
			int size;

			if (e->bEndpointAddress != GVUSB2_AUDIO_EP ||
			    !usb_endpoint_xfer_isoc(e))
				continue;

			size = usb_endpoint_maxp(e) * usb_endpoint_maxp_mult(e);
			if (size > *packet_size) {
				*packet_size = size;
				*alt = as->desc.bAlternateSetting;
			}
		}
	}

	return *alt < 0 ? -ENODEV : 0;
}

static void gvusb2_snd_card_free(struct snd_card *card)
{
	struct gvusb2_snd *dev = card->private_data;

	gvusb2_snd_free_urbs(dev);
	gvusb2_free(&dev->gv);
}

static int gvusb2_snd_probe(struct usb_interface *intf,
	const struct usb_device_id *id_entry)
{
	struct usb_device *udev = interface_to_usbdev(intf);
	struct snd_card *card;
	struct snd_pcm *pcm;
	struct gvusb2_snd *dev;
	int alt, packet_size, ret;

	if (intf->altsetting[0].desc.bInterfaceClass == USB_CLASS_AUDIO)
		return -ENODEV;

	if (gvusb2_snd_find_altsetting(intf, &alt, &packet_size) < 0)
		return -ENODEV;

	ret = snd_card_new(&intf->dev, index, id ? id : "GVUSB2", THIS_MODULE,
		sizeof(*dev), &card);
	if (ret < 0)
		return ret;

	dev = card->private_data;
	dev->card = card;
	dev->intf = intf;
	dev->packet_size = packet_size;
	mutex_init(&dev->urb_lock);
	spin_lock_init(&dev->lock);
	gvusb2_init(&dev->gv, udev);
	card->private_free = gvusb2_snd_card_free;

	ret = usb_set_interface(udev,
		intf->cur_altsetting->desc.bInterfaceNumber, alt);
	if (ret < 0) {
		dev_err(&intf->dev, "can't select audio altsetting %d (%d)\n",
			alt, ret);
		goto free_card;
	}

	ret = gvusb2_snd_reset_adc(&dev->gv);
	if (ret < 0)
		goto free_card;

	ret = gvusb2_snd_allocate_urbs(dev);
	if (ret < 0)
		goto free_card;

	ret = snd_pcm_new(card, "GV-USB2 Line In", 0, 0, 1, &pcm);
	if (ret < 0)
		goto free_card;

	pcm->private_data = dev;
	strscpy(pcm->name, "GV-USB2 Line In", sizeof(pcm->name));
	snd_pcm_set_ops(pcm, SNDRV_PCM_STREAM_CAPTURE, &gvusb2_snd_capture_ops);
	snd_pcm_set_managed_buffer_all(pcm, SNDRV_DMA_TYPE_VMALLOC, NULL, 0, 0);

	strscpy(card->driver, "gvusb2", sizeof(card->driver));
	strscpy(card->shortname, CARD_NAME, sizeof(card->shortname));
	snprintf(card->longname, sizeof(card->longname), "I-O DATA %s at ",
		CARD_NAME);
	usb_make_path(udev, card->longname + strlen(card->longname),
		sizeof(card->longname) - strlen(card->longname));

	ret = snd_card_register(card);
	if (ret < 0)
		goto free_card;

	usb_set_intfdata(intf, dev);
	dev_info(&intf->dev, "GV-USB2: audio altsetting %d, %d bytes per packet, card %d\n",
		alt, packet_size, card->number);

	return 0;

free_card:
	snd_card_free(card);
	return ret;
}

static void gvusb2_snd_disconnect(struct usb_interface *intf)
{
	struct gvusb2_snd *dev = usb_get_intfdata(intf);

	if (dev == NULL)
		return;

	usb_set_intfdata(intf, NULL);

	snd_card_disconnect(dev->card);

	mutex_lock(&dev->urb_lock);
	dev->disconnected = true;
	mutex_unlock(&dev->urb_lock);
	gvusb2_snd_kill_urbs(dev);

	snd_card_free_when_closed(dev->card);
}

struct usb_driver gvusb2_snd_usb_driver = {
	.name = "gvusb2-snd",
	.probe = gvusb2_snd_probe,
	.disconnect = gvusb2_snd_disconnect,
	.id_table = gvusb2_id_table,
};
