# The portable C core (see PORTING.md). `make` builds it for the Python tools.
CC      ?= clang
CFLAGS  ?= -O2 -std=c99 -Wall -Wextra -Wno-unused-parameter
SRC     := core/cpu.c core/mem.c
HDR     := core/arm.h core/arm_internal.h

build/libv600core.dylib: $(SRC) $(HDR)
	@mkdir -p build
	$(CC) $(CFLAGS) -dynamiclib -o $@ $(SRC)

test: build/libv600core.dylib
	V600_CPU=c .venv/bin/python tools/selftest.py

check: build/libv600core.dylib          # C core vs Unicorn, state compared every 0.25 s
	.venv/bin/python tools/lockstep.py 20.0 0.25

clean:
	rm -rf build

.PHONY: test check clean
