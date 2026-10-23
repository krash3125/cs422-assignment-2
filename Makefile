obj-m += tcp_qaimd.o

KDIR ?= $(HOME)/WSL2-Linux-Kernel
PWD  := $(shell pwd)

all:
	$(MAKE) KBUILD_MODPOST_WARN=1 -C $(KDIR) M=$(PWD) modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean

load: all
	sudo modprobe tcp_bbr
	-sudo rmmod tcp_qaimd 2>/dev/null || true
	sudo insmod tcp_qaimd.ko
	sysctl net.ipv4.tcp_available_congestion_control

unload:
	sudo rmmod tcp_qaimd