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
#include <linux/usb.h>

#include "gvusb2.h"

MODULE_DESCRIPTION("I-O DATA GV-USB2 video and audio capture driver");
MODULE_AUTHOR("Isaac Lozano <109lozanoi@gmail.com>");
MODULE_LICENSE("Dual BSD/GPL");
MODULE_VERSION("2.0");

const struct usb_device_id gvusb2_id_table[] = {
	{ USB_DEVICE(GVUSB2_VENDOR_ID, GVUSB2_PRODUCT_ID) },
	{ }
};
MODULE_DEVICE_TABLE(usb, gvusb2_id_table);

static int __init gvusb2_module_init(void)
{
	int ret;

	ret = usb_register(&gvusb2_vid_usb_driver);
	if (ret)
		return ret;

	ret = usb_register(&gvusb2_snd_usb_driver);
	if (ret)
		usb_deregister(&gvusb2_vid_usb_driver);

	return ret;
}

static void __exit gvusb2_module_exit(void)
{
	usb_deregister(&gvusb2_snd_usb_driver);
	usb_deregister(&gvusb2_vid_usb_driver);
}

module_init(gvusb2_module_init);
module_exit(gvusb2_module_exit);
