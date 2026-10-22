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

#include <linux/delay.h>
#include <linux/usb.h>

#include "gvusb2.h"
#include "gvusb2-vid.h"

static int gvusb2_i2c_busy_wait(struct gvusb2_vid *dev, u8 wait_mask)
{
	int attempts, ret;
	u8 flag;

	for (attempts = 0; attempts < 100; attempts++) {
		ret = gvusb2_read_reg(&dev->gv, 0x0201, &flag);
		if (ret < 0)
			return ret;
		if (flag & wait_mask)
			return 0;
		usleep_range(50, 100);
	}

	return -ETIMEDOUT;
}

static int gvusb2_i2c_read_reg(struct gvusb2_vid *dev, u8 addr, u8 reg,
	u8 *value)
{
	int ret;

	ret = gvusb2_write_reg(&dev->gv, 0x0203, (addr << 1) | 1);
	if (ret < 0)
		return ret;

	ret = gvusb2_write_reg(&dev->gv, 0x0208, reg);
	if (ret < 0)
		return ret;

	ret = gvusb2_write_reg(&dev->gv, 0x0200, 0x60);
	if (ret < 0)
		return ret;

	ret = gvusb2_i2c_busy_wait(dev, 0x01);
	if (ret < 0)
		return ret;

	return gvusb2_read_reg(&dev->gv, 0x0209, value);
}

static int gvusb2_i2c_write_reg(struct gvusb2_vid *dev, u8 addr, u8 reg,
	u8 value)
{
	int ret;

	ret = gvusb2_write_reg(&dev->gv, 0x0203, addr << 1);
	if (ret < 0)
		return ret;

	ret = gvusb2_write_reg(&dev->gv, 0x0204, reg);
	if (ret < 0)
		return ret;

	ret = gvusb2_write_reg(&dev->gv, 0x0205, value);
	if (ret < 0)
		return ret;

	ret = gvusb2_write_reg(&dev->gv, 0x0200, 0x05);
	if (ret < 0)
		return ret;

	return gvusb2_i2c_busy_wait(dev, 0x04);
}

int gvusb2_tw_read(struct gvusb2_vid *dev, u8 reg, u8 *value)
{
	int ret;

	mutex_lock(&dev->i2c_lock);
	ret = gvusb2_i2c_read_reg(dev, GVUSB2_TW9910_ADDR, reg, value);
	mutex_unlock(&dev->i2c_lock);
	if (ret < 0)
		dev_dbg(&dev->intf->dev, "tw9910 read 0x%02x failed (%d)\n",
			reg, ret);

	return ret;
}

int gvusb2_tw_write(struct gvusb2_vid *dev, u8 reg, u8 value)
{
	int ret;

	mutex_lock(&dev->i2c_lock);
	ret = gvusb2_i2c_write_reg(dev, GVUSB2_TW9910_ADDR, reg, value);
	mutex_unlock(&dev->i2c_lock);
	if (ret < 0)
		dev_dbg(&dev->intf->dev, "tw9910 write 0x%02x failed (%d)\n",
			reg, ret);

	return ret;
}

int gvusb2_tw_mask(struct gvusb2_vid *dev, u8 reg, u8 mask, u8 value)
{
	int ret;
	u8 val;

	ret = gvusb2_tw_read(dev, reg, &val);
	if (ret < 0)
		return ret;

	return gvusb2_tw_write(dev, reg, (val & ~mask) | (value & mask));
}

void gvusb2_i2c_init(struct gvusb2_vid *dev)
{
	mutex_init(&dev->i2c_lock);

	gvusb2_write_reg(&dev->gv, 0x0202, 0x08);

	gvusb2_write_reg(&dev->gv, 0x02ff, 0x00);
}
