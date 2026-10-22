obj-m += gvusb2.o
gvusb2-y := gvusb2-main.o gvusb2-core.o gvusb2-snd.o \
	gvusb2-vid.o gvusb2-v4l2.o gvusb2-i2c.o

KDIR ?= /lib/modules/$(shell uname -r)/build

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean

install: all
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules_install
	depmod -a
	-modprobe -r gvusb2
	modprobe gvusb2

.PHONY: all clean install
