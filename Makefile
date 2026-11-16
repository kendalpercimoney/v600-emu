# The portable C core (see PORTING.md). `make` builds it for the Python tools.
CC      ?= clang
CFLAGS  ?= -O2 -std=c99 -Wall -Wextra -Wno-unused-parameter
SRC     := $(wildcard core/*.c)
HDR     := $(wildcard core/*.h)

build/libv600core.dylib: $(SRC) $(HDR)
	@mkdir -p build
	$(CC) $(CFLAGS) -dynamiclib -o $@ $(SRC)

test: build/libv600core.dylib
	.venv/bin/python tools/selftest.py

check: build/libv600core.dylib          # whole machine vs the golden trace (tests/)
	.venv/bin/python tools/golden.py check

clean:
	rm -rf build

.PHONY: test check clean
