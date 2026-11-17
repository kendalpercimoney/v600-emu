# The portable C core (see PORTING.md). `make` builds it for the Python tools;
# `make wasm` for the browser and the iPhone app (build/web/, needs the Emscripten SDK).
-include local.cfg                     # git-ignored: APP=..., EMSDK=..., EMSDK_PYTHON=...
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

api-test: build/api_test          # the C interface alone (core/v600.h)
	build/api_test firmware/v600.shx

build/api_test: $(SRC) $(HDR) tests/api_test.c
	@mkdir -p build out
	$(CC) $(CFLAGS) -o $@ $(SRC) tests/api_test.c -lm

# -- WebAssembly: build/web/v600core.{js,wasm}, the public interface (core/v600.h) only ----
comma        := ,
space        := $() $()
EMSDK        ?= $(HOME)/code/emsdk
EMSDK_PYTHON ?= $(firstword $(wildcard /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12) python3)
EMCC         := EM_CONFIG=$(EMSDK)/.emscripten EMSDK_PYTHON=$(EMSDK_PYTHON) $(EMSDK)/upstream/emscripten/emcc
API          := v600_create v600_destroy v600_run v600_seconds v600_set_time v600_key v600_flip v600_lcd \
                v600_lcd_serial v600_outer v600_outer_serial v600_audio v600_media \
                v600_state_ready v600_state_changes v600_save_state v600_set_log malloc free
EMFLAGS      := -O3 -flto -std=c99 -Wall -Wno-unused-parameter \
                -sMODULARIZE -sEXPORT_NAME=V600Core -sENVIRONMENT=web,worker \
                -sINITIAL_MEMORY=192MB -sMAXIMUM_MEMORY=512MB -sALLOW_MEMORY_GROWTH \
                -sSTACK_SIZE=1MB -sALLOW_TABLE_GROWTH \
                -sEXPORTED_FUNCTIONS=$(subst $(space),$(comma),$(addprefix _,$(API))) \
                -sEXPORTED_RUNTIME_METHODS=HEAPU8,HEAP16,HEAPU32,addFunction,UTF8ToString

wasm: build/web/v600core.js

# The page with the WebAssembly core: build/web/ = web/ + the core (+ the firmware, gzipped,
# when firmware/v600.shx exists: for private builds only, never published).
web: build/web/v600core.js $(wildcard web/*) $(if $(wildcard firmware/v600.shx),build/web/firmware.shx.gz)
	cp web/* build/web/

wasm-test: build/web/v600core.js     # the WebAssembly build in Node: boot, a tap, speed
	node tests/wasm_test.mjs

build/web/firmware.shx.gz: firmware/v600.shx
	@mkdir -p build/web
	gzip -9 -c $< > $@

build/web/v600core.js: $(SRC) $(HDR)
	@mkdir -p build/web
	$(EMCC) $(EMFLAGS) -o $@ $(SRC)

clean:
	rm -rf build

.PHONY: test check api-test wasm web wasm-test clean
