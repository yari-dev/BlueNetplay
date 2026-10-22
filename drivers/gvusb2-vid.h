/* SPDX-License-Identifier: GPL-2.0-only OR BSD-3-Clause */
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

#ifndef __GVUSB2_VID_H__
#define __GVUSB2_VID_H__

#include <linux/mutex.h>
#include <media/v4l2-device.h>
#include <media/v4l2-ctrls.h>
#include <media/videobuf2-core.h>
#include <media/videobuf2-v4l2.h>

#include "gvusb2.h"

#define GVUSB2_INPUT_COMPOSITE 0
#define GVUSB2_INPUT_SVIDEO    1
#define GVUSB2_INPUT_MAX_VAL   1

#define GVUSB2_CID_BASE			(V4L2_CID_USER_BASE | 0xf000)
#define GVUSB2_CID_VERTICAL_START	(GVUSB2_CID_BASE + 0)
#define GVUSB2_CID_HORIZONTAL_START	(GVUSB2_CID_BASE + 1)

#define GVUSB2_TW9910_ADDR 0x44

struct gvusb2_vb {
	struct vb2_v4l2_buffer vb;
	struct list_head list;
	int buf_pos;
	int line_pos;
	int field;
};

#define GVUSB2_HAS_TOP    1
#define GVUSB2_HAS_BOTTOM 2

struct gvusb2_vid {
	struct gvusb2_dev gv;
	bool disconnected;

	struct usb_interface *intf;
	int alt;
	int packet_size;
	struct urb *urbs[GVUSB2_VID_URBS];
	unsigned char counter;
	u64 packet_time;

	struct mutex i2c_lock;

	struct v4l2_device v4l2_dev;
	struct video_device vdev;
	struct v4l2_ctrl_handler ctrl_handler;
	struct v4l2_ctrl *ctrl_vstart;
	struct v4l2_ctrl *ctrl_hstart;
	struct mutex lock;
	unsigned int input_num;
	unsigned int sequence;
	v4l2_std_id standard;
	enum v4l2_field field_mode;
	int width;
	int field_lines;

	struct vb2_queue vb2q;
	struct list_head buf_list;
	spinlock_t buf_list_lock;
	struct gvusb2_vb *current_buf;
};

static inline int gvusb2_buf_height(struct gvusb2_vid *dev)
{
	return dev->field_mode == V4L2_FIELD_ALTERNATE ?
		dev->field_lines : dev->field_lines * 2;
}

static inline bool gvusb2_is_50hz(struct gvusb2_vid *dev)
{
	return !(dev->standard & V4L2_STD_525_60);
}

void gvusb2_release(struct v4l2_device *v4l2_dev);
int gvusb2_vid_submit_urbs(struct gvusb2_vid *dev);
void gvusb2_vid_cancel_urbs(struct gvusb2_vid *dev);

void gvusb2_vid_return_buffers(struct gvusb2_vid *dev,
	enum vb2_buffer_state state);
int gvusb2_vb2_setup(struct gvusb2_vid *dev);
int gvusb2_v4l2_register(struct gvusb2_vid *dev);
int gvusb2_video_register(struct gvusb2_vid *dev);

int gvusb2_tw_read(struct gvusb2_vid *dev, u8 reg, u8 *value);
int gvusb2_tw_write(struct gvusb2_vid *dev, u8 reg, u8 value);
int gvusb2_tw_mask(struct gvusb2_vid *dev, u8 reg, u8 mask, u8 value);
void gvusb2_i2c_init(struct gvusb2_vid *dev);

#endif
