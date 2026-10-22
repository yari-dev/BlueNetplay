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
#include <linux/slab.h>
#include <linux/usb.h>
#include <media/v4l2-dev.h>

#include "gvusb2-vid.h"

static struct gvusb2_vb *gvusb2_vid_next_buffer(struct gvusb2_vid *dev)
{
	struct gvusb2_vb *buf = NULL;
	unsigned long flags;

	spin_lock_irqsave(&dev->buf_list_lock, flags);

	if (!list_empty(&dev->buf_list)) {
		buf = list_first_entry(&dev->buf_list, struct gvusb2_vb, list);
		list_del(&buf->list);
		buf->buf_pos = 0;
		buf->line_pos = 0;
		buf->field = 0;
	}

	spin_unlock_irqrestore(&dev->buf_list_lock, flags);

	return buf;
}

static void gvusb2_vid_copy_video(struct gvusb2_vid *dev, u8 *buf, int len)
{
	struct gvusb2_vb *vb = dev->current_buf;
	u32 buffer_len = vb2_plane_size(&vb->vb.vb2_buf, 0);
	u8 *buffer_addr = vb2_plane_vaddr(&vb->vb.vb2_buf, 0);
	int bytes_per_line = dev->width * 2;
	int skip = dev->field_mode == V4L2_FIELD_ALTERNATE ? 0 : bytes_per_line;

	while (len > 0) {
		int len_to_copy = min(len, bytes_per_line - vb->line_pos);

		if (vb->buf_pos + len_to_copy > buffer_len) {
			gvusb2_dbg(&dev->intf->dev, "buffer overflow, dropping %d bytes\n",
				len);
			return;
		}

		memcpy(buffer_addr + vb->buf_pos, buf, len_to_copy);
		vb->buf_pos += len_to_copy;
		vb->line_pos += len_to_copy;
		len -= len_to_copy;
		buf += len_to_copy;

		if (vb->line_pos == bytes_per_line) {
			vb->buf_pos += skip;
			vb->line_pos = 0;
		}
	}
}

static void gvusb2_vid_submit_video_buffer(struct gvusb2_vid *dev)
{
	struct gvusb2_vb *vb = dev->current_buf;

	vb2_set_plane_payload(&vb->vb.vb2_buf, 0,
		vb2_plane_size(&vb->vb.vb2_buf, 0));

	vb->vb.sequence = dev->sequence++;
	vb->vb.field = dev->field_mode == V4L2_FIELD_ALTERNATE ?
		vb->field : V4L2_FIELD_INTERLACED;
	vb->vb.vb2_buf.timestamp = dev->packet_time;
	vb2_buffer_done(&vb->vb.vb2_buf, VB2_BUF_STATE_DONE);

	dev->current_buf = gvusb2_vid_next_buffer(dev);
}

static void gvusb2_vid_process_status_packet(struct gvusb2_vid *dev,
	u8 status_byte)
{
	bool odd = status_byte & 0x40;
	struct gvusb2_vb *vb;

	if (dev->field_mode == V4L2_FIELD_ALTERNATE) {
		if (dev->current_buf == NULL)
			dev->current_buf = gvusb2_vid_next_buffer(dev);
		else if (dev->current_buf->buf_pos > 0)
			gvusb2_vid_submit_video_buffer(dev);

		vb = dev->current_buf;
		if (vb) {
			vb->buf_pos = 0;
			vb->line_pos = 0;
			vb->field = odd ? V4L2_FIELD_TOP : V4L2_FIELD_BOTTOM;
		}
		return;
	}

	if (dev->current_buf == NULL) {
		dev->current_buf = gvusb2_vid_next_buffer(dev);
	} else if (odd || (dev->current_buf->field & GVUSB2_HAS_BOTTOM)) {
		if (dev->current_buf->field)
			gvusb2_vid_submit_video_buffer(dev);
	}

	vb = dev->current_buf;
	if (vb == NULL)
		return;

	vb->buf_pos = odd ? 0 : dev->width * 2;
	vb->line_pos = 0;
	vb->field |= odd ? GVUSB2_HAS_TOP : GVUSB2_HAS_BOTTOM;
}

static void gvusb2_vid_process_data(struct gvusb2_vid *dev, u8 *buf, int len)
{
	if (len == 0)
		return;

	if (len < 4) {
		gvusb2_dbg(&dev->intf->dev, "video packet too small (%d)\n", len);
		return;
	}

	if (buf[0] & 0x80) {
		dev->counter = (dev->counter + 1) % 64;

		gvusb2_vid_process_status_packet(dev, buf[0]);
	} else {
		if (dev->counter != buf[1])
			gvusb2_dbg(&dev->intf->dev,
				"counter desync. expected %d got %d\n",
				dev->counter, buf[1]);

		dev->counter = (buf[1] + 1) % 64;

		if (dev->current_buf != NULL && dev->current_buf->field)
			gvusb2_vid_copy_video(dev, buf + 4, len - 4);
	}
}

static void gvusb2_vid_process_isoc(struct gvusb2_vid *dev, struct urb *urb)
{
	u64 now = ktime_get_ns();
	int i;

	for (i = 0; i < urb->number_of_packets; i++) {
		int status = urb->iso_frame_desc[i].status;

		dev->packet_time = now -
			(u64)(urb->number_of_packets - 1 - i) * 125 * NSEC_PER_USEC;

		if (status < 0) {
			gvusb2_dbg(&dev->intf->dev,
				"iso packet failed, skipping (%d)\n", status);
		} else {
			u8 *data = urb->transfer_buffer +
				urb->iso_frame_desc[i].offset;
			int len = urb->iso_frame_desc[i].actual_length;

			gvusb2_vid_process_data(dev, data, len);
		}
	}
}

static void gvusb2_vid_isoc_irq(struct urb *urb)
{
	int i, ret;
	struct gvusb2_vid *dev = urb->context;

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
		gvusb2_dbg(&dev->intf->dev, "urb error %d\n", urb->status);
		goto resubmit;
	}

	gvusb2_vid_process_isoc(dev, urb);

resubmit:
	for (i = 0; i < urb->number_of_packets; i++) {
		urb->iso_frame_desc[i].status = 0;
		urb->iso_frame_desc[i].actual_length = 0;
	}

	ret = usb_submit_urb(urb, GFP_ATOMIC);
	if (ret && ret != -EPERM && ret != -ENODEV)
		dev_err_ratelimited(&dev->intf->dev,
			"urb resubmit failed (%d)\n", ret);
}

static void gvusb2_vid_free_urbs(struct gvusb2_vid *dev)
{
	int i;

	for (i = 0; i < GVUSB2_VID_URBS; i++) {
		struct urb *urb = dev->urbs[i];

		if (urb != NULL) {
			kfree(urb->transfer_buffer);
			usb_free_urb(urb);
			dev->urbs[i] = NULL;
		}
	}
}

static int gvusb2_vid_allocate_urbs(struct gvusb2_vid *dev)
{
	int i;
	int size = GVUSB2_VID_PACKETS * dev->packet_size;

	for (i = 0; i < GVUSB2_VID_URBS; i++) {
		struct urb *urb;
		int pidx;

		urb = usb_alloc_urb(GVUSB2_VID_PACKETS, GFP_KERNEL);
		if (urb == NULL)
			goto nomem;
		dev->urbs[i] = urb;

		urb->transfer_buffer = kzalloc(size, GFP_KERNEL);
		if (urb->transfer_buffer == NULL)
			goto nomem;

		urb->dev = dev->gv.udev;
		urb->pipe = usb_rcvisocpipe(dev->gv.udev, GVUSB2_VIDEO_EP);
		urb->transfer_buffer_length = size;
		urb->complete = gvusb2_vid_isoc_irq;
		urb->context = dev;
		urb->interval = 1;
		urb->start_frame = 0;
		urb->number_of_packets = GVUSB2_VID_PACKETS;
		urb->transfer_flags = URB_ISO_ASAP;

		for (pidx = 0; pidx < GVUSB2_VID_PACKETS; pidx++) {
			urb->iso_frame_desc[pidx].offset = pidx * dev->packet_size;
			urb->iso_frame_desc[pidx].length = dev->packet_size;
		}
	}

	return 0;

nomem:
	gvusb2_vid_free_urbs(dev);
	return -ENOMEM;
}

int gvusb2_vid_submit_urbs(struct gvusb2_vid *dev)
{
	int i;
	int ret;

	dev->counter = 0;
	for (i = 0; i < GVUSB2_VID_URBS; i++) {
		ret = usb_submit_urb(dev->urbs[i], GFP_KERNEL);
		if (ret < 0) {
			dev_err(&dev->intf->dev, "urb submit failed (%d)\n", ret);
			gvusb2_vid_cancel_urbs(dev);
			return ret;
		}
	}

	return 0;
}

void gvusb2_vid_cancel_urbs(struct gvusb2_vid *dev)
{
	int i;

	for (i = 0; i < GVUSB2_VID_URBS; i++)
		usb_kill_urb(dev->urbs[i]);
}

struct regval {
	u16 reg;
	u8 val;
};

static void gvusb2_stk1150_reset_gpio(struct gvusb2_vid *dev)
{
	gvusb2_set_reg_mask(&dev->gv, 0x0002, 0x08, 0x08);
	gvusb2_set_reg_mask(&dev->gv, 0x0000, 0x08, 0x08);
}

static void gvusb2_stk1150_init_inner(struct gvusb2_vid *dev)
{
	int i;

	static const struct regval phase3[] = {
		{0x000d, 0x00},
		{0x000f, 0x00},
		{0x0018, 0x10},
		{0x001b, 0x0e},
		{0x001c, 0x46},
		{0x001a, 0x14},
		{0x0019, 0x00},
		{0x0300, 0x12},
		{0x0350, 0x41},
		{0x0351, 0x00},
		{0x0352, 0x00},
		{0x0353, 0x00},
		{0x0300, 0x80},
		{0x0018, 0x10},
		{0x0103, 0x00},
		{0x0100, 0x33},
		{0x0202, 0x08},

		{0xffff, 0xff}
	};

	for (i = 0; phase3[i].reg != 0xffff; i++)
		gvusb2_write_reg(&dev->gv, phase3[i].reg, phase3[i].val);
}

static void gvusb2_stk1150_reset_slave_ics(struct gvusb2_vid *dev)
{
	u8 reg_0000;

	if (gvusb2_read_reg(&dev->gv, 0x0000, &reg_0000) < 0)
		return;

	gvusb2_write_reg(&dev->gv, 0x0000, reg_0000 & 0xf7);
	usleep_range(10 * USEC_PER_MSEC, 20 * USEC_PER_MSEC);
	gvusb2_write_reg(&dev->gv, 0x0000, (reg_0000 & 0xf7) | 0x08);
	usleep_range(10 * USEC_PER_MSEC, 20 * USEC_PER_MSEC);
}

static void gvusb2_stk1150_init(struct gvusb2_vid *dev)
{
	gvusb2_stk1150_reset_gpio(dev);
	gvusb2_stk1150_reset_slave_ics(dev);
	gvusb2_stk1150_init_inner(dev);
}

static int gvusb2_vid_find_altsetting(struct usb_interface *intf,
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

			if (e->bEndpointAddress != GVUSB2_VIDEO_EP ||
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

void gvusb2_release(struct v4l2_device *v4l2_dev)
{
	struct gvusb2_vid *dev =
		container_of(v4l2_dev, struct gvusb2_vid, v4l2_dev);

	v4l2_ctrl_handler_free(&dev->ctrl_handler);
	v4l2_device_unregister(&dev->v4l2_dev);

	gvusb2_vid_free_urbs(dev);
	gvusb2_free(&dev->gv);

	mutex_destroy(&dev->i2c_lock);
	mutex_destroy(&dev->lock);

	kfree(dev);
}

static int gvusb2_vid_probe(struct usb_interface *intf,
	const struct usb_device_id *id)
{
	struct usb_device *udev = interface_to_usbdev(intf);
	struct gvusb2_vid *dev;
	int alt, packet_size, ret;
	u8 chip_id = 0;

	if (gvusb2_vid_find_altsetting(intf, &alt, &packet_size) < 0)
		return -ENODEV;

	dev = kzalloc(sizeof(*dev), GFP_KERNEL);
	if (dev == NULL)
		return -ENOMEM;

	gvusb2_init(&dev->gv, udev);
	dev->intf = intf;
	dev->alt = alt;
	dev->packet_size = packet_size;
	mutex_init(&dev->lock);
	spin_lock_init(&dev->buf_list_lock);
	INIT_LIST_HEAD(&dev->buf_list);

	ret = usb_set_interface(udev,
		intf->cur_altsetting->desc.bInterfaceNumber, alt);
	if (ret < 0) {
		dev_err(&intf->dev, "can't select video altsetting %d (%d)\n",
			alt, ret);
		goto free_dev;
	}

	gvusb2_stk1150_init(dev);
	gvusb2_i2c_init(dev);

	gvusb2_tw_read(dev, 0x00, &chip_id);
	dev_info(&intf->dev, "GV-USB2: video altsetting %d, %d bytes per microframe, decoder id 0x%02x\n",
		alt, packet_size, chip_id);

	ret = gvusb2_vid_allocate_urbs(dev);
	if (ret < 0)
		goto free_dev;

	ret = gvusb2_vb2_setup(dev);
	if (ret < 0)
		goto free_urbs;

	ret = gvusb2_v4l2_register(dev);
	if (ret < 0)
		goto free_urbs;

	usb_set_intfdata(intf, dev);

	ret = gvusb2_video_register(dev);
	if (ret < 0) {
		usb_set_intfdata(intf, NULL);
		v4l2_device_put(&dev->v4l2_dev);
		return ret;
	}

	return 0;

free_urbs:
	gvusb2_vid_free_urbs(dev);
free_dev:
	gvusb2_free(&dev->gv);
	mutex_destroy(&dev->lock);
	kfree(dev);

	return ret;
}

static void gvusb2_vid_disconnect(struct usb_interface *intf)
{
	struct gvusb2_vid *dev = usb_get_intfdata(intf);

	if (dev == NULL)
		return;

	usb_set_intfdata(intf, NULL);

	mutex_lock(&dev->lock);

	dev->disconnected = true;
	gvusb2_vid_cancel_urbs(dev);
	vb2_queue_error(&dev->vb2q);
	gvusb2_vid_return_buffers(dev, VB2_BUF_STATE_ERROR);

	video_unregister_device(&dev->vdev);
	v4l2_device_disconnect(&dev->v4l2_dev);

	mutex_unlock(&dev->lock);

	v4l2_device_put(&dev->v4l2_dev);
}

struct usb_driver gvusb2_vid_usb_driver = {
	.name = "gvusb2-vid",
	.probe = gvusb2_vid_probe,
	.disconnect = gvusb2_vid_disconnect,
	.id_table = gvusb2_id_table,
};
