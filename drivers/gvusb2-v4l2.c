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

#include <linux/kernel.h>
#include <linux/usb.h>
#include <linux/version.h>
#include <linux/videodev2.h>
#include <media/v4l2-dev.h>
#include <media/v4l2-event.h>
#include <media/v4l2-ioctl.h>
#include <media/videobuf2-v4l2.h>
#include <media/videobuf2-core.h>
#include <media/videobuf2-vmalloc.h>

#include "gvusb2-vid.h"

#define TW_ID      0x00
#define TW_STATUS1 0x01
#define TW_INFORM  0x02
#define TW_OPFORM  0x03
#define TW_CROP_HI 0x07
#define TW_VDELAY  0x08
#define TW_VACTIVE 0x09
#define TW_HDELAY  0x0a
#define TW_HACTIVE 0x0b
#define TW_SDT     0x1c
#define TW_VVBI    0x55

#define TW_STATUS1_VDLOSS 0x80

struct i2c_regval {
	u8 reg;
	u8 val;
};

static void gvusb2_update_geometry(struct gvusb2_vid *dev)
{
	dev->width = 720;
	dev->field_lines = gvusb2_is_50hz(dev) ? 288 : 240;
}

static void gvusb2_write_window(struct gvusb2_vid *dev, int v, int h)
{
	int vend = v + dev->field_lines;
	int hend = h + dev->width * 2;

	gvusb2_write_reg(&dev->gv, 0x0110, h & 0xff);
	gvusb2_write_reg(&dev->gv, 0x0111, h >> 8);
	gvusb2_write_reg(&dev->gv, 0x0112, v & 0xff);
	gvusb2_write_reg(&dev->gv, 0x0113, v >> 8);
	gvusb2_write_reg(&dev->gv, 0x0114, hend & 0xff);
	gvusb2_write_reg(&dev->gv, 0x0115, hend >> 8);
	gvusb2_write_reg(&dev->gv, 0x0116, vend & 0xff);
	gvusb2_write_reg(&dev->gv, 0x0117, vend >> 8);
}

static void gvusb2_set_window(struct gvusb2_vid *dev)
{
	gvusb2_write_window(dev, dev->ctrl_vstart->cur.val,
		dev->ctrl_hstart->cur.val);
}

static void gvusb2_tw9910_set_std(struct gvusb2_vid *dev)
{
	bool pal = gvusb2_is_50hz(dev);
	u8 vdelay = pal ? 0x18 : 0x13;
	u16 vactive = pal ? 0x124 : 0xf4;

	gvusb2_tw_mask(dev, TW_VVBI, 0x10, pal ? 0x00 : 0x10);

	gvusb2_tw_mask(dev, TW_CROP_HI, 0xf0, (vactive >> 4) & 0x30);
	gvusb2_tw_write(dev, TW_VDELAY, vdelay);
	gvusb2_tw_write(dev, TW_VACTIVE, vactive & 0xff);
	gvusb2_tw_write(dev, TW_HDELAY, 0x12);
	gvusb2_tw_write(dev, TW_HACTIVE, 0xd2);
}

static void gvusb2_fill_pix_format(struct gvusb2_vid *dev,
	struct v4l2_pix_format *pix, enum v4l2_field field)
{
	pix->width = dev->width;
	pix->height = field == V4L2_FIELD_ALTERNATE ?
		dev->field_lines : dev->field_lines * 2;
	pix->field = field;
	pix->pixelformat = V4L2_PIX_FMT_UYVY;
	pix->bytesperline = pix->width * 2;
	pix->sizeimage = pix->bytesperline * pix->height;
	pix->colorspace = V4L2_COLORSPACE_SMPTE170M;
	pix->ycbcr_enc = V4L2_YCBCR_ENC_DEFAULT;
	pix->quantization = V4L2_QUANTIZATION_DEFAULT;
	pix->xfer_func = V4L2_XFER_FUNC_DEFAULT;
	pix->priv = 0;
}

static enum v4l2_field gvusb2_pick_field(struct gvusb2_vid *dev,
	struct v4l2_pix_format *pix)
{
	switch (pix->field) {
	case V4L2_FIELD_ALTERNATE:
	case V4L2_FIELD_TOP:
	case V4L2_FIELD_BOTTOM:
		return V4L2_FIELD_ALTERNATE;
	case V4L2_FIELD_INTERLACED:
		return V4L2_FIELD_INTERLACED;
	default:
		return pix->height && pix->height <= dev->field_lines ?
			V4L2_FIELD_ALTERNATE : V4L2_FIELD_INTERLACED;
	}
}

void gvusb2_vid_return_buffers(struct gvusb2_vid *dev,
	enum vb2_buffer_state state)
{
	struct gvusb2_vb *buf;
	unsigned long flags;

	spin_lock_irqsave(&dev->buf_list_lock, flags);
	while (!list_empty(&dev->buf_list)) {
		buf = list_first_entry(&dev->buf_list,
			struct gvusb2_vb, list);
		list_del(&buf->list);
		vb2_buffer_done(&buf->vb.vb2_buf, state);
	}

	if (dev->current_buf) {
		vb2_buffer_done(&dev->current_buf->vb.vb2_buf, state);
		dev->current_buf = NULL;
	}
	spin_unlock_irqrestore(&dev->buf_list_lock, flags);
}

static int gvusb2_vb2_queue_setup(struct vb2_queue *vb2q,
	unsigned int *nbuffers,
	unsigned int *nplanes,
	unsigned int sizes[],
	struct device *alloc_devs[])
{
	struct gvusb2_vid *dev = vb2_get_drv_priv(vb2q);
	unsigned long size = dev->width * gvusb2_buf_height(dev) * 2;

	if (*nbuffers < 4)
		*nbuffers = 4;

	if (*nplanes) {
		if (*nplanes != 1 || sizes[0] < size)
			return -EINVAL;
		return 0;
	}

	*nplanes = 1;
	sizes[0] = size;

	return 0;
}

static void gvusb2_vb2_buf_queue(struct vb2_buffer *vb)
{
	unsigned long flags;
	struct gvusb2_vid *dev = vb2_get_drv_priv(vb->vb2_queue);
	struct vb2_v4l2_buffer *vbuf = to_vb2_v4l2_buffer(vb);
	struct gvusb2_vb *gvusb2_vbuf =
		container_of(vbuf, struct gvusb2_vb, vb);

	spin_lock_irqsave(&dev->buf_list_lock, flags);
	list_add_tail(&gvusb2_vbuf->list, &dev->buf_list);
	spin_unlock_irqrestore(&dev->buf_list_lock, flags);
}

static int gvusb2_vb2_start_streaming(struct vb2_queue *vb2q,
	unsigned int count)
{
	int ret;
	struct gvusb2_vid *dev = vb2_get_drv_priv(vb2q);

	if (dev->disconnected) {
		ret = -ENODEV;
		goto return_buffers;
	}

	dev->sequence = 0;
	dev->current_buf = NULL;

	gvusb2_tw9910_set_std(dev);
	gvusb2_set_window(dev);

	gvusb2_write_reg(&dev->gv, 0x0100, 0xb3);
	gvusb2_write_reg(&dev->gv, 0x0103, 0x00);

	ret = gvusb2_vid_submit_urbs(dev);
	if (ret < 0) {
		gvusb2_write_reg(&dev->gv, 0x0100, 0x33);
		goto return_buffers;
	}

	return 0;

return_buffers:
	gvusb2_vid_return_buffers(dev, VB2_BUF_STATE_QUEUED);
	return ret;
}

static void gvusb2_vb2_stop_streaming(struct vb2_queue *vb2q)
{
	struct gvusb2_vid *dev = vb2_get_drv_priv(vb2q);

	gvusb2_vid_cancel_urbs(dev);

	if (!dev->disconnected) {
		gvusb2_write_reg(&dev->gv, 0x0100, 0x33);
		gvusb2_write_reg(&dev->gv, 0x0103, 0x00);
	}

	gvusb2_vid_return_buffers(dev, VB2_BUF_STATE_ERROR);
}

static const struct vb2_ops gvusb2_vb2_ops = {
	.queue_setup     = gvusb2_vb2_queue_setup,
	.buf_queue       = gvusb2_vb2_buf_queue,
	.start_streaming = gvusb2_vb2_start_streaming,
	.stop_streaming  = gvusb2_vb2_stop_streaming,
#if LINUX_VERSION_CODE < KERNEL_VERSION(6, 18, 0)
	.wait_prepare    = vb2_ops_wait_prepare,
	.wait_finish     = vb2_ops_wait_finish,
#endif
};

int gvusb2_vb2_setup(struct gvusb2_vid *dev)
{
	struct vb2_queue *vb2q = &dev->vb2q;

	vb2q->type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
	vb2q->io_modes = VB2_MMAP | VB2_READ | VB2_USERPTR | VB2_DMABUF;
	vb2q->drv_priv = dev;
	vb2q->buf_struct_size = sizeof(struct gvusb2_vb);
	vb2q->ops = &gvusb2_vb2_ops;
	vb2q->mem_ops = &vb2_vmalloc_memops;
	vb2q->lock = &dev->lock;
	vb2q->timestamp_flags = V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC;
	vb2q->dev = &dev->intf->dev;

	return vb2_queue_init(vb2q);
}

static int gvusb2_s_ctrl(struct v4l2_ctrl *ctrl)
{
	struct gvusb2_vid *dev = container_of(ctrl->handler,
		struct gvusb2_vid, ctrl_handler);

	switch (ctrl->id) {
	case V4L2_CID_BRIGHTNESS:
		return gvusb2_tw_write(dev, 0x10, (ctrl->val + 0x80) & 0xff);
	case V4L2_CID_CONTRAST:
		return gvusb2_tw_write(dev, 0x11, ctrl->val);
	case V4L2_CID_SATURATION:
		gvusb2_tw_write(dev, 0x13, ctrl->val);
		return gvusb2_tw_write(dev, 0x14, ctrl->val);
	case V4L2_CID_HUE:
		return gvusb2_tw_write(dev, 0x15, (ctrl->val + 0x80) & 0xff);
	case V4L2_CID_SHARPNESS:
		return gvusb2_tw_write(dev, 0x12, (ctrl->val & 0x0f) | 0x50);
	case GVUSB2_CID_VERTICAL_START:
		gvusb2_write_window(dev, ctrl->val, dev->ctrl_hstart->cur.val);
		return 0;
	case GVUSB2_CID_HORIZONTAL_START:
		gvusb2_write_window(dev, dev->ctrl_vstart->cur.val, ctrl->val);
		return 0;
	}

	return -EINVAL;
}

static const struct v4l2_ctrl_ops gvusb2_ctrl_ops = {
	.s_ctrl = gvusb2_s_ctrl,
};

static const struct v4l2_ctrl_config gvusb2_ctrl_vertical = {
	.ops = &gvusb2_ctrl_ops,
	.id = GVUSB2_CID_VERTICAL_START,
	.name = "Vertical Start",
	.type = V4L2_CTRL_TYPE_INTEGER,
	.flags = V4L2_CTRL_FLAG_SLIDER,
	.min = 1,
	.max = 4,
	.step = 1,
	.def = 2,
};

static const struct v4l2_ctrl_config gvusb2_ctrl_horizontal = {
	.ops = &gvusb2_ctrl_ops,
	.id = GVUSB2_CID_HORIZONTAL_START,
	.name = "Horizontal Start",
	.type = V4L2_CTRL_TYPE_INTEGER,
	.flags = V4L2_CTRL_FLAG_SLIDER,
	.min = 0,
	.max = 8,
	.step = 4,
	.def = 4,
};

static int gvusb2_vidioc_querycap(struct file *file, void *priv,
	struct v4l2_capability *cap)
{
	struct gvusb2_vid *dev = video_drvdata(file);

	strscpy(cap->driver, "gvusb2", sizeof(cap->driver));
	strscpy(cap->card, "I-O DATA GV-USB2", sizeof(cap->card));
	usb_make_path(dev->gv.udev, cap->bus_info, sizeof(cap->bus_info));

	return 0;
}

static int gvusb2_vidioc_enum_input(struct file *file, void *priv,
	struct v4l2_input *i)
{
	struct gvusb2_vid *dev = video_drvdata(file);
	u8 status;

	switch (i->index) {
	case GVUSB2_INPUT_COMPOSITE:
		strscpy(i->name, "Composite", sizeof(i->name));
		break;
	case GVUSB2_INPUT_SVIDEO:
		strscpy(i->name, "S-Video", sizeof(i->name));
		break;
	default:
		return -EINVAL;
	}

	i->type = V4L2_INPUT_TYPE_CAMERA;
	i->capabilities = V4L2_IN_CAP_STD;
	i->std = dev->vdev.tvnorms;

	if (i->index == dev->input_num &&
	    gvusb2_tw_read(dev, TW_STATUS1, &status) == 0 &&
	    (status & TW_STATUS1_VDLOSS))
		i->status = V4L2_IN_ST_NO_SIGNAL;

	return 0;
}

static int gvusb2_vidioc_g_input(struct file *file, void *priv, unsigned int *i)
{
	struct gvusb2_vid *dev = video_drvdata(file);

	*i = dev->input_num;

	return 0;
}

static int gvusb2_vidioc_s_input(struct file *file, void *priv, unsigned int i)
{
	struct gvusb2_vid *dev = video_drvdata(file);
	u8 val;
	int ret;

	switch (i) {
	case GVUSB2_INPUT_COMPOSITE:
		val = 0x00;
		break;
	case GVUSB2_INPUT_SVIDEO:
		val = 0x14;
		break;
	default:
		return -EINVAL;
	}

	ret = gvusb2_tw_mask(dev, TW_INFORM, 0x3c, val);
	if (ret < 0)
		return ret;

	dev->input_num = i;

	return 0;
}

static int gvusb2_vidioc_querystd(struct file *file, void *priv,
	v4l2_std_id *std)
{
	struct gvusb2_vid *dev = video_drvdata(file);
	u8 status, sdt;

	if (gvusb2_tw_read(dev, TW_STATUS1, &status) < 0 ||
	    gvusb2_tw_read(dev, TW_SDT, &sdt) < 0)
		return -EIO;

	if (status & TW_STATUS1_VDLOSS) {
		*std = V4L2_STD_UNKNOWN;
		return 0;
	}

	switch ((sdt >> 4) & 0x07) {
	case 0:
		*std &= V4L2_STD_NTSC_M;
		break;
	case 1:
		*std &= V4L2_STD_PAL;
		break;
	case 2:
		*std &= V4L2_STD_SECAM;
		break;
	case 3:
		*std &= V4L2_STD_NTSC_443;
		break;
	case 4:
		*std &= V4L2_STD_PAL_M;
		break;
	case 5:
		*std &= V4L2_STD_PAL_N | V4L2_STD_PAL_Nc;
		break;
	case 6:
		*std &= V4L2_STD_PAL_60;
		break;
	default:
		break;
	}

	return 0;
}

static int gvusb2_vidioc_g_std(struct file *file, void *priv, v4l2_std_id *std)
{
	struct gvusb2_vid *dev = video_drvdata(file);

	*std = dev->standard;

	return 0;
}

static int gvusb2_vidioc_s_std(struct file *file, void *priv, v4l2_std_id std)
{
	struct gvusb2_vid *dev = video_drvdata(file);

	if (std == dev->standard)
		return 0;

	if (vb2_is_busy(&dev->vb2q))
		return -EBUSY;

	dev->standard = std;
	gvusb2_update_geometry(dev);
	gvusb2_tw9910_set_std(dev);
	gvusb2_set_window(dev);

	return 0;
}

static int gvusb2_vidioc_enum_fmt_vid_cap(struct file *file, void *priv,
	struct v4l2_fmtdesc *f)
{
	if (f->index != 0)
		return -EINVAL;

	f->pixelformat = V4L2_PIX_FMT_UYVY;

	return 0;
}

static int gvusb2_vidioc_g_fmt_vid_cap(struct file *file, void *priv,
	struct v4l2_format *f)
{
	struct gvusb2_vid *dev = video_drvdata(file);

	gvusb2_fill_pix_format(dev, &f->fmt.pix, dev->field_mode);

	return 0;
}

static int gvusb2_vidioc_try_fmt_vid_cap(struct file *file, void *priv,
	struct v4l2_format *f)
{
	struct gvusb2_vid *dev = video_drvdata(file);

	gvusb2_fill_pix_format(dev, &f->fmt.pix,
		gvusb2_pick_field(dev, &f->fmt.pix));

	return 0;
}

static int gvusb2_vidioc_s_fmt_vid_cap(struct file *file, void *priv,
	struct v4l2_format *f)
{
	struct gvusb2_vid *dev = video_drvdata(file);
	enum v4l2_field field = gvusb2_pick_field(dev, &f->fmt.pix);

	if (vb2_is_busy(&dev->vb2q))
		return -EBUSY;

	dev->field_mode = field;
	gvusb2_fill_pix_format(dev, &f->fmt.pix, field);

	return 0;
}

static int gvusb2_vidioc_enum_framesizes(struct file *file, void *priv,
	struct v4l2_frmsizeenum *fsize)
{
	struct gvusb2_vid *dev = video_drvdata(file);

	if (fsize->pixel_format != V4L2_PIX_FMT_UYVY || fsize->index > 1)
		return -EINVAL;

	fsize->type = V4L2_FRMSIZE_TYPE_DISCRETE;
	fsize->discrete.width = dev->width;
	fsize->discrete.height = fsize->index == 0 ?
		dev->field_lines * 2 : dev->field_lines;

	return 0;
}

static void gvusb2_frame_interval(struct gvusb2_vid *dev, bool fields,
	struct v4l2_fract *tpf)
{
	if (gvusb2_is_50hz(dev)) {
		tpf->numerator = 1;
		tpf->denominator = fields ? 50 : 25;
	} else {
		tpf->numerator = 1001;
		tpf->denominator = fields ? 60000 : 30000;
	}
}

static int gvusb2_vidioc_enum_frameintervals(struct file *file, void *priv,
	struct v4l2_frmivalenum *fival)
{
	struct gvusb2_vid *dev = video_drvdata(file);

	if (fival->index != 0 || fival->pixel_format != V4L2_PIX_FMT_UYVY ||
	    fival->width != dev->width)
		return -EINVAL;

	if (fival->height == dev->field_lines)
		gvusb2_frame_interval(dev, true, &fival->discrete);
	else if (fival->height == dev->field_lines * 2)
		gvusb2_frame_interval(dev, false, &fival->discrete);
	else
		return -EINVAL;

	fival->type = V4L2_FRMIVAL_TYPE_DISCRETE;

	return 0;
}

static int gvusb2_vidioc_g_parm(struct file *file, void *priv,
	struct v4l2_streamparm *parm)
{
	struct gvusb2_vid *dev = video_drvdata(file);

	if (parm->type != V4L2_BUF_TYPE_VIDEO_CAPTURE)
		return -EINVAL;

	memset(&parm->parm.capture, 0, sizeof(parm->parm.capture));
	parm->parm.capture.capability = V4L2_CAP_TIMEPERFRAME;
	parm->parm.capture.readbuffers = 4;
	gvusb2_frame_interval(dev, dev->field_mode == V4L2_FIELD_ALTERNATE,
		&parm->parm.capture.timeperframe);

	return 0;
}

static int gvusb2_vidioc_s_parm(struct file *file, void *priv,
	struct v4l2_streamparm *parm)
{
	struct gvusb2_vid *dev = video_drvdata(file);
	struct v4l2_fract *tpf = &parm->parm.capture.timeperframe;

	if (parm->type != V4L2_BUF_TYPE_VIDEO_CAPTURE)
		return -EINVAL;

	if (tpf->numerator && tpf->denominator && !vb2_is_busy(&dev->vb2q)) {
		u64 mfps = div_u64((u64)tpf->denominator * 1000, tpf->numerator);
		u64 mid = gvusb2_is_50hz(dev) ? 37500 : 45000;

		dev->field_mode = mfps > mid ?
			V4L2_FIELD_ALTERNATE : V4L2_FIELD_INTERLACED;
	}

	return gvusb2_vidioc_g_parm(file, priv, parm);
}

static const struct v4l2_ioctl_ops gvusb2_v4l2_ioctl_ops = {
	.vidioc_querycap          = gvusb2_vidioc_querycap,
	.vidioc_enum_input        = gvusb2_vidioc_enum_input,
	.vidioc_g_input           = gvusb2_vidioc_g_input,
	.vidioc_s_input           = gvusb2_vidioc_s_input,
	.vidioc_querystd          = gvusb2_vidioc_querystd,
	.vidioc_g_std             = gvusb2_vidioc_g_std,
	.vidioc_s_std             = gvusb2_vidioc_s_std,
	.vidioc_enum_fmt_vid_cap  = gvusb2_vidioc_enum_fmt_vid_cap,
	.vidioc_g_fmt_vid_cap     = gvusb2_vidioc_g_fmt_vid_cap,
	.vidioc_s_fmt_vid_cap     = gvusb2_vidioc_s_fmt_vid_cap,
	.vidioc_try_fmt_vid_cap   = gvusb2_vidioc_try_fmt_vid_cap,
	.vidioc_enum_framesizes   = gvusb2_vidioc_enum_framesizes,
	.vidioc_enum_frameintervals = gvusb2_vidioc_enum_frameintervals,
	.vidioc_g_parm            = gvusb2_vidioc_g_parm,
	.vidioc_s_parm            = gvusb2_vidioc_s_parm,

	.vidioc_reqbufs           = vb2_ioctl_reqbufs,
	.vidioc_querybuf          = vb2_ioctl_querybuf,
	.vidioc_qbuf              = vb2_ioctl_qbuf,
	.vidioc_dqbuf             = vb2_ioctl_dqbuf,
	.vidioc_create_bufs       = vb2_ioctl_create_bufs,
	.vidioc_prepare_buf       = vb2_ioctl_prepare_buf,
	.vidioc_streamon          = vb2_ioctl_streamon,
	.vidioc_streamoff         = vb2_ioctl_streamoff,
	.vidioc_expbuf            = vb2_ioctl_expbuf,

	.vidioc_log_status        = v4l2_ctrl_log_status,
	.vidioc_subscribe_event   = v4l2_ctrl_subscribe_event,
	.vidioc_unsubscribe_event = v4l2_event_unsubscribe,
};

int gvusb2_v4l2_register(struct gvusb2_vid *dev)
{
	int i;
	int ret;

	static const struct i2c_regval phase6[] = {
			{0x06, 0x00},
			{0x03, 0xa2},
			{0x05, 0x01},
			{0x19, 0xde},
			{0x1a, 0x0f},
			{0x1b, 0x00},
			{0x6e, 0x28},
			{0x06, 0x80},
			{0xff, 0xff}
	};

	dev->standard = V4L2_STD_NTSC_M;
	dev->field_mode = V4L2_FIELD_INTERLACED;
	gvusb2_update_geometry(dev);

	for (i = 0; phase6[i].reg != 0xff; i++)
		gvusb2_tw_write(dev, phase6[i].reg, phase6[i].val);

	gvusb2_tw9910_set_std(dev);

	gvusb2_set_reg_mask(&dev->gv, 0x05f0, 0x08, 0x08);

	ret = v4l2_ctrl_handler_init(&dev->ctrl_handler, 7);
	if (ret < 0)
		return ret;

	v4l2_ctrl_new_std(&dev->ctrl_handler, &gvusb2_ctrl_ops,
		V4L2_CID_BRIGHTNESS, 0, 255, 1, 128);
	v4l2_ctrl_new_std(&dev->ctrl_handler, &gvusb2_ctrl_ops,
		V4L2_CID_CONTRAST, 0, 255, 1, 128);
	v4l2_ctrl_new_std(&dev->ctrl_handler, &gvusb2_ctrl_ops,
		V4L2_CID_SATURATION, 0, 255, 1, 128);
	v4l2_ctrl_new_std(&dev->ctrl_handler, &gvusb2_ctrl_ops,
		V4L2_CID_HUE, 0, 255, 1, 128);
	v4l2_ctrl_new_std(&dev->ctrl_handler, &gvusb2_ctrl_ops,
		V4L2_CID_SHARPNESS, 0, 15, 1, 0);
	dev->ctrl_vstart = v4l2_ctrl_new_custom(&dev->ctrl_handler,
		&gvusb2_ctrl_vertical, NULL);
	dev->ctrl_hstart = v4l2_ctrl_new_custom(&dev->ctrl_handler,
		&gvusb2_ctrl_horizontal, NULL);

	if (dev->ctrl_handler.error) {
		ret = dev->ctrl_handler.error;
		goto free_ctrl_handler;
	}

	v4l2_ctrl_handler_setup(&dev->ctrl_handler);

	dev->v4l2_dev.release = gvusb2_release;
	dev->v4l2_dev.ctrl_handler = &dev->ctrl_handler;
	ret = v4l2_device_register(&dev->intf->dev, &dev->v4l2_dev);
	if (ret < 0)
		goto free_ctrl_handler;

	return 0;

free_ctrl_handler:
	v4l2_ctrl_handler_free(&dev->ctrl_handler);

	return ret;
}

static const struct v4l2_file_operations gvusb2_v4l2_fops = {
	.owner = THIS_MODULE,
	.open = v4l2_fh_open,
	.release = vb2_fop_release,
	.read = vb2_fop_read,
	.poll = vb2_fop_poll,
	.mmap = vb2_fop_mmap,
	.unlocked_ioctl = video_ioctl2,
};

static const struct video_device gvusb2_video_device_template = {
	.name = "gvusb2",
	.device_caps = V4L2_CAP_VIDEO_CAPTURE | V4L2_CAP_STREAMING |
		V4L2_CAP_READWRITE,
	.tvnorms = V4L2_STD_525_60 | V4L2_STD_625_50,
	.vfl_dir = VFL_DIR_RX,
	.fops = &gvusb2_v4l2_fops,
	.ioctl_ops = &gvusb2_v4l2_ioctl_ops,
	.release = video_device_release_empty,
};

int gvusb2_video_register(struct gvusb2_vid *dev)
{
	dev->vdev = gvusb2_video_device_template;

	dev->vdev.v4l2_dev = &dev->v4l2_dev;
	dev->vdev.queue = &dev->vb2q;
	dev->vdev.lock = &dev->lock;

	video_set_drvdata(&dev->vdev, dev);

	return video_register_device(&dev->vdev, VFL_TYPE_VIDEO, -1);
}
