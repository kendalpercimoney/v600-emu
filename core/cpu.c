/* The instruction interpreter: ARMv5T (ARM and Thumb), big-endian.

   Where the architecture leaves a choice, this follows what Unicorn (QEMU's ARM926 model)
   does, since that is what the emulator was developed on: PC reads as +8 / +4 everywhere
   (also for register-specified shifts and stores of PC), unaligned loads are plain
   unaligned reads, loads into PC interwork (v5), data-processing writes to PC do not,
   an STM storing its base register stores the original value. */
#include "arm_internal.h"

static inline uint32_t ror32(uint32_t v, unsigned n) {
    n &= 31;
    return n ? (v >> n) | (v << (32 - n)) : v;
}

static int cond_ok(uint32_t cpsr, unsigned c) {
    int n = cpsr >> 31 & 1, z = cpsr >> 30 & 1, cf = cpsr >> 29 & 1, v = cpsr >> 28 & 1;
    switch (c) {
    case 0: return z;           case 1: return !z;
    case 2: return cf;          case 3: return !cf;
    case 4: return n;           case 5: return !n;
    case 6: return v;           case 7: return !v;
    case 8: return cf && !z;    case 9: return !cf || z;
    case 10: return n == v;     case 11: return n != v;
    case 12: return !z && n == v;
    case 13: return z || n != v;
    default: return 1;
    }
}

static inline void set_nz(arm_t *s, uint32_t r) {
    s->cpsr = (s->cpsr & ~(FLAG_N | FLAG_Z)) | (r & FLAG_N) | (r ? 0 : FLAG_Z);
}

static inline void set_nzc(arm_t *s, uint32_t r, uint32_t c) {
    s->cpsr = (s->cpsr & ~(FLAG_N | FLAG_Z | FLAG_C)) | (r & FLAG_N) | (r ? 0 : FLAG_Z) | (c ? FLAG_C : 0);
}

/* a + b + c, setting NZCV when asked; subtraction is a + ~b + 1 */
static inline uint32_t add_flags(arm_t *s, uint32_t a, uint32_t b, uint32_t c, int setf) {
    uint64_t wide = (uint64_t)a + b + c;
    uint32_t r = (uint32_t)wide;
    if (setf) {
        uint32_t f = s->cpsr & 0x0FFFFFFF;
        f |= r & FLAG_N;
        if (!r) f |= FLAG_Z;
        if (wide >> 32) f |= FLAG_C;
        if ((~(a ^ b) & (a ^ r)) >> 31) f |= FLAG_V;
        s->cpsr = f;
    }
    return r;
}

static inline int carry(arm_t *s) { return s->cpsr >> 29 & 1; }

/* The status bits an ARMv5 core lets instructions change (NZCVQ, A, I, F, T, mode), as in
   QEMU: bit 9 (E, which Unicorn uses for big-endian data) stays as it is. */
#define PSR_V5 0xF80001FFu
static inline void exception_return(arm_t *s) {
    write_cpsr(s, (s->cpsr & ~PSR_V5) | (s->spsr & PSR_V5));
}

/* Branch-and-exchange: bit 0 picks Thumb. */
static inline void bx(arm_t *s, uint32_t v) {
    if (v & 1) { s->cpsr |= T_BIT; s->next_pc = v & ~1u; }
    else { s->cpsr &= ~T_BIT; s->next_pc = v & ~3u; }
}

static inline void undefined(arm_t *s) { s->err = ERR_INSN_INVALID; s->stop = 1; }

/* Shifts by an amount from a register (also Thumb's shift-by-register ALU ops). */
static uint32_t shift_reg(uint32_t v, unsigned type, unsigned amt, uint32_t *c) {
    if (amt == 0) return v;
    switch (type) {
    case 0:                                                   /* LSL */
        if (amt < 32) { *c = v >> (32 - amt) & 1; return v << amt; }
        *c = amt == 32 ? v & 1 : 0; return 0;
    case 1:                                                   /* LSR */
        if (amt < 32) { *c = v >> (amt - 1) & 1; return v >> amt; }
        *c = amt == 32 ? v >> 31 : 0; return 0;
    case 2:                                                   /* ASR */
        if (amt < 32) { *c = v >> (amt - 1) & 1; return (uint32_t)((int32_t)v >> amt); }
        *c = v >> 31; return *c ? 0xFFFFFFFF : 0;
    default:                                                  /* ROR */
        if ((amt & 31) == 0) { *c = v >> 31; return v; }
        *c = v >> ((amt & 31) - 1) & 1; return ror32(v, amt);
    }
}

/* Shifts by an immediate amount (0 means 32 for LSR / ASR, RRX for ROR). */
static uint32_t shift_imm(uint32_t v, unsigned type, unsigned amt, uint32_t *c) {
    switch (type) {
    case 0:
        if (amt) { *c = v >> (32 - amt) & 1; v <<= amt; }
        return v;
    case 1:
        if (!amt) { *c = v >> 31; return 0; }
        *c = v >> (amt - 1) & 1; return v >> amt;
    case 2:
        if (!amt) { *c = v >> 31; return *c ? 0xFFFFFFFF : 0; }
        *c = v >> (amt - 1) & 1; return (uint32_t)((int32_t)v >> amt);
    default:
        if (!amt) { uint32_t r = (v >> 1) | (*c << 31); *c = v & 1; return r; }
        *c = v >> (amt - 1) & 1; return ror32(v, amt);
    }
}

/* ---- ARM -------------------------------------------------------------------------- */
#define RV(n) ((n) == 15 ? pc + 8 : s->r[n])

static inline void load_reg(arm_t *s, unsigned rd, uint32_t v) {
    if (rd == 15) bx(s, v);                                   /* v5: loads interwork */
    else s->r[rd] = v;
}

static uint32_t user_reg(arm_t *s, unsigned i) {
    int m = mode_index(s->cpsr);
    if (i >= 8 && i <= 12 && m == 1) return s->usr8[i - 8];
    if (i == 13 && m) return s->r13b[0];
    if (i == 14 && m) return s->r14b[0];
    return s->r[i];
}

static void set_user_reg(arm_t *s, unsigned i, uint32_t v) {
    int m = mode_index(s->cpsr);
    if (i >= 8 && i <= 12 && m == 1) s->usr8[i - 8] = v;
    else if (i == 13 && m) s->r13b[0] = v;
    else if (i == 14 && m) s->r14b[0] = v;
    else s->r[i] = v;
}

static void psr_transfer(arm_t *s, uint32_t insn, uint32_t pc) {
    int spsr = insn >> 22 & 1;
    if (!(insn & 0x00200000)) {                               /* MRS */
        s->r[insn >> 12 & 15] = spsr ? s->spsr : s->cpsr;
        return;
    }
    uint32_t v = insn & 0x02000000 ? ror32(insn & 0xFF, (insn >> 8 & 15) * 2) : RV(insn & 15);
    uint32_t mask = 0;
    if (insn & 0x80000) mask |= 0xFF000000;
    if (insn & 0x40000) mask |= 0x00FF0000;
    if (insn & 0x20000) mask |= 0x0000FF00;
    if (insn & 0x10000) mask |= 0x000000FF;
    mask &= PSR_V5;
    if (spsr) { s->spsr = (s->spsr & ~mask) | (v & mask); return; }
    if (mode_index(s->cpsr) == 0 && (s->cpsr & 0x1F) == 0x10)
        mask &= 0xFF000000;                                   /* user mode: flags only */
    mask &= ~T_BIT;                                           /* MSR cannot change state */
    write_cpsr(s, (s->cpsr & ~mask) | (v & mask));
}

static void data_processing(arm_t *s, uint32_t insn, uint32_t pc) {
    unsigned op = insn >> 21 & 15, S = insn >> 20 & 1, rn = insn >> 16 & 15, rd = insn >> 12 & 15;
    uint32_t c = carry(s), b;
    if (insn & 0x02000000) {
        unsigned rot = (insn >> 8 & 15) * 2;
        b = ror32(insn & 0xFF, rot);
        if (rot) c = b >> 31;
    } else if (insn & 0x10) {
        b = shift_reg(RV(insn & 15), insn >> 5 & 3, RV(insn >> 8 & 15) & 0xFF, &c);
    } else {
        b = shift_imm(RV(insn & 15), insn >> 5 & 3, insn >> 7 & 31, &c);
    }
    uint32_t a = RV(rn), r;
    int setf = S && rd != 15, logical = 0, write = 1;
    switch (op) {
    case 0:  r = a & b; logical = 1; break;
    case 1:  r = a ^ b; logical = 1; break;
    case 2:  r = add_flags(s, a, ~b, 1, setf); break;
    case 3:  r = add_flags(s, b, ~a, 1, setf); break;
    case 4:  r = add_flags(s, a, b, 0, setf); break;
    case 5:  r = add_flags(s, a, b, carry(s), setf); break;
    case 6:  r = add_flags(s, a, ~b, carry(s), setf); break;
    case 7:  r = add_flags(s, b, ~a, carry(s), setf); break;
    case 8:  r = a & b; logical = 1; write = 0; break;
    case 9:  r = a ^ b; logical = 1; write = 0; break;
    case 10: r = add_flags(s, a, ~b, 1, S); write = 0; break;
    case 11: r = add_flags(s, a, b, 0, S); write = 0; break;
    case 12: r = a | b; logical = 1; break;
    case 13: r = b; logical = 1; break;
    case 14: r = a & ~b; logical = 1; break;
    default: r = ~b; logical = 1; break;
    }
    if (logical && (setf || (!write && S)))
        set_nzc(s, r, c);
    if (!write)
        return;
    if (rd != 15) { s->r[rd] = r; return; }
    if (S) exception_return(s);                            /* exception return */
    s->next_pc = r & (s->cpsr & T_BIT ? ~1u : ~3u);
}

static void multiply(arm_t *s, uint32_t insn) {
    unsigned rd = insn >> 16 & 15, rn = insn >> 12 & 15, rs = insn >> 8 & 15, rm = insn & 15;
    uint32_t r = s->r[rm] * s->r[rs] + (insn & 0x00200000 ? s->r[rn] : 0);
    s->r[rd] = r;
    if (insn & 0x00100000) set_nz(s, r);
}

static void multiply_long(arm_t *s, uint32_t insn) {
    unsigned hi = insn >> 16 & 15, lo = insn >> 12 & 15, rs = insn >> 8 & 15, rm = insn & 15;
    uint64_t r = insn & 0x00400000
        ? (uint64_t)((int64_t)(int32_t)s->r[rm] * (int32_t)s->r[rs])
        : (uint64_t)s->r[rm] * s->r[rs];
    if (insn & 0x00200000) r += (uint64_t)s->r[hi] << 32 | s->r[lo];
    s->r[lo] = (uint32_t)r;
    s->r[hi] = (uint32_t)(r >> 32);
    if (insn & 0x00100000)
        s->cpsr = (s->cpsr & ~(FLAG_N | FLAG_Z)) | ((uint32_t)(r >> 32) & FLAG_N) | (r ? 0 : FLAG_Z);
}

static void halfword_transfer(arm_t *s, uint32_t insn, uint32_t pc) {
    unsigned P = insn >> 24 & 1, U = insn >> 23 & 1, W = insn >> 21 & 1, L = insn >> 20 & 1;
    unsigned rn = insn >> 16 & 15, rd = insn >> 12 & 15, sh = insn >> 5 & 3;
    uint32_t off = insn & 0x00400000 ? ((insn >> 4) & 0xF0) | (insn & 0xF) : s->r[insn & 15];
    uint32_t base = RV(rn), moved = U ? base + off : base - off, ea = P ? moved : base;
    uint32_t store = RV(rd), store2 = rd < 15 ? s->r[rd + 1] : 0;
    if (!P || W) s->r[rn] = moved;
    if (L) {
        uint32_t v = sh == 1 ? mem_load(s, ea, 2)
                   : sh == 2 ? (uint32_t)(int32_t)(int8_t)mem_load(s, ea, 1)
                   : (uint32_t)(int32_t)(int16_t)mem_load(s, ea, 2);
        load_reg(s, rd, v);
    } else if (sh == 1) {
        mem_store(s, ea, 2, store);
    } else if (sh == 2) {                                     /* LDRD */
        uint32_t v0 = mem_load(s, ea, 4), v1 = mem_load(s, ea + 4, 4);
        s->r[rd] = v0;
        load_reg(s, rd + 1, v1);
    } else {                                                  /* STRD */
        mem_store(s, ea, 4, store);
        mem_store(s, ea + 4, 4, store2);
    }
}

static void single_transfer(arm_t *s, uint32_t insn, uint32_t pc) {
    unsigned P = insn >> 24 & 1, U = insn >> 23 & 1, B = insn >> 22 & 1, W = insn >> 21 & 1;
    unsigned L = insn >> 20 & 1, rn = insn >> 16 & 15, rd = insn >> 12 & 15;
    uint32_t off;
    if (insn & 0x02000000) {
        if (insn & 0x10) { undefined(s); return; }
        uint32_t c = carry(s);
        off = shift_imm(RV(insn & 15), insn >> 5 & 3, insn >> 7 & 31, &c);
    } else {
        off = insn & 0xFFF;
    }
    uint32_t base = RV(rn), moved = U ? base + off : base - off, ea = P ? moved : base;
    uint32_t store = RV(rd);
    if (!P || W) s->r[rn] = moved;
    if (L) load_reg(s, rd, B ? mem_load(s, ea, 1) : mem_load(s, ea, 4));
    else mem_store(s, ea, B ? 1 : 4, B ? store & 0xFF : store);
}

static void block_transfer(arm_t *s, uint32_t insn, uint32_t pc) {
    unsigned P = insn >> 24 & 1, U = insn >> 23 & 1, S = insn >> 22 & 1, W = insn >> 21 & 1;
    unsigned L = insn >> 20 & 1, rn = insn >> 16 & 15, list = insn & 0xFFFF;
    unsigned n = __builtin_popcount(list);
    uint32_t base = s->r[rn];
    uint32_t a = U ? base + (P ? 4 : 0) : base - 4 * n + (P ? 0 : 4);
    uint32_t final = U ? base + 4 * n : base - 4 * n;
    if (!L) {
        for (unsigned i = 0; i < 16; i++) {
            if (!(list >> i & 1)) continue;
            uint32_t v = i == 15 ? pc + 8 : S ? user_reg(s, i) : s->r[i];
            mem_store(s, a, 4, v);
            a += 4;
        }
        if (W) s->r[rn] = final;
        return;
    }
    if (W) s->r[rn] = final;                                  /* a loaded base wins */
    for (unsigned i = 0; i < 16; i++) {
        if (!(list >> i & 1)) continue;
        uint32_t v = mem_load(s, a, 4);
        a += 4;
        if (i == 15) {
            if (S) {                                          /* return from exception */
                exception_return(s);
                s->next_pc = v & (s->cpsr & T_BIT ? ~1u : ~3u);
            } else {
                bx(s, v);
            }
        } else if (S && !(list & 0x8000)) {
            set_user_reg(s, i, v);
        } else {
            s->r[i] = v;
        }
    }
}

static void coprocessor(arm_t *s, uint32_t insn) {
    if ((insn >> 8 & 15) != 15 || !(insn & 0x10)) { undefined(s); return; }
    unsigned rd = insn >> 12 & 15, crn = insn >> 16 & 15;
    if (insn & 0x00100000) {                                  /* MRC p15 */
        uint32_t v = crn == 0 ? 0x41069265 : crn == 1 ? s->cp15_control : 0;
        if (rd == 15) s->cpsr = (s->cpsr & 0x0FFFFFFF) | (v & 0xF0000000);
        else s->r[rd] = v;
    } else if (crn == 1) {                                    /* MCR p15, c1 */
        s->cp15_control = s->r[rd];
    }
}

static void exec_arm(arm_t *s, uint32_t insn, uint32_t pc) {
    unsigned cond = insn >> 28;
    if (cond == 15) {
        if ((insn & 0x0E000000) == 0x0A000000) {              /* BLX immediate */
            int32_t off = ((int32_t)(insn << 8) >> 6) | (insn >> 23 & 2);
            s->r[14] = pc + 4;
            s->cpsr |= T_BIT;
            s->next_pc = pc + 8 + off;
        } else if ((insn & 0x0D70F000) != 0x0550F000) {       /* PLD is a no-op */
            undefined(s);
        }
        return;
    }
    if (!cond_ok(s->cpsr, cond))
        return;
    switch (insn >> 25 & 7) {
    case 0:
        if ((insn & 0x0FC000F0) == 0x00000090) { multiply(s, insn); return; }
        if ((insn & 0x0F8000F0) == 0x00800090) { multiply_long(s, insn); return; }
        if ((insn & 0x0FB00FF0) == 0x01000090) {              /* SWP / SWPB */
            unsigned n = insn & 0x00400000 ? 1 : 4;
            uint32_t a = s->r[insn >> 16 & 15], old = mem_load(s, a, n);
            mem_store(s, a, n, s->r[insn & 15]);
            s->r[insn >> 12 & 15] = old;
            return;
        }
        if ((insn & 0x90) == 0x90) { halfword_transfer(s, insn, pc); return; }
        if ((insn & 0x01900000) == 0x01000000) {              /* misc: TST.. with S = 0 */
            if ((insn & 0x0FFFFFF0) == 0x012FFF10) { bx(s, RV(insn & 15)); return; }
            if ((insn & 0x0FFFFFF0) == 0x012FFF30) {          /* BLX register */
                uint32_t t = RV(insn & 15);
                s->r[14] = pc + 4;
                bx(s, t);
                return;
            }
            if ((insn & 0x0FFF0FF0) == 0x016F0F10) {          /* CLZ */
                uint32_t v = s->r[insn & 15];
                s->r[insn >> 12 & 15] = v ? __builtin_clz(v) : 32;
                return;
            }
            if ((insn & 0x0FF000F0) == 0x01200070) { s->intr = 7; return; }   /* BKPT */
            if ((insn & 0x0FB00FF0) == 0x01000000 || (insn & 0x0FB0FFF0) == 0x0120F000) {
                psr_transfer(s, insn, pc);
                return;
            }
            undefined(s);
            return;
        }
        data_processing(s, insn, pc);
        return;
    case 1:
        if ((insn & 0x01900000) == 0x01000000) {
            if ((insn & 0x0FB0F000) == 0x0320F000) psr_transfer(s, insn, pc);
            else undefined(s);
            return;
        }
        data_processing(s, insn, pc);
        return;
    case 2: case 3:
        single_transfer(s, insn, pc);
        return;
    case 4:
        block_transfer(s, insn, pc);
        return;
    case 5:
        if (insn & 0x01000000) s->r[14] = pc + 4;
        s->next_pc = pc + 8 + ((int32_t)(insn << 8) >> 6);
        return;
    case 6:
        undefined(s);
        return;
    default:
        if (insn & 0x01000000) s->intr = 2;                   /* SWI */
        else coprocessor(s, insn);
        return;
    }
}
#undef RV

/* ---- Thumb ------------------------------------------------------------------------ */
#define TV(n) ((n) == 15 ? pc + 4 : s->r[n])

static void thumb_alu(arm_t *s, uint32_t insn) {
    unsigned op = insn >> 6 & 15, rs = insn >> 3 & 7, rd = insn & 7;
    uint32_t a = s->r[rd], b = s->r[rs], c = carry(s), r;
    switch (op) {
    case 0:  r = a & b; set_nz(s, r); break;
    case 1:  r = a ^ b; set_nz(s, r); break;
    case 2: case 3: case 4: case 7:                           /* LSL LSR ASR ROR by register */
        r = shift_reg(a, op == 2 ? 0 : op == 3 ? 1 : op == 4 ? 2 : 3, b & 0xFF, &c);
        set_nzc(s, r, c);
        break;
    case 5:  r = add_flags(s, a, b, carry(s), 1); break;
    case 6:  r = add_flags(s, a, ~b, carry(s), 1); break;
    case 8:  set_nz(s, a & b); return;
    case 9:  r = add_flags(s, 0, ~b, 1, 1); break;            /* NEG */
    case 10: add_flags(s, a, ~b, 1, 1); return;
    case 11: add_flags(s, a, b, 0, 1); return;
    case 12: r = a | b; set_nz(s, r); break;
    case 13: r = a * b; set_nz(s, r); break;
    case 14: r = a & ~b; set_nz(s, r); break;
    default: r = ~b; set_nz(s, r); break;
    }
    s->r[rd] = r;
}

static void exec_thumb(arm_t *s, uint32_t insn, uint32_t pc) {
    switch (insn >> 11) {
    case 0: case 1: case 2: {                                 /* shift by immediate */
        uint32_t c = carry(s), r = shift_imm(s->r[insn >> 3 & 7], insn >> 11, insn >> 6 & 31, &c);
        s->r[insn & 7] = r;
        set_nzc(s, r, c);
        return;
    }
    case 3: {                                                 /* ADD / SUB, 3 operands */
        unsigned n = insn >> 6 & 7;
        uint32_t a = s->r[insn >> 3 & 7], b = insn & 0x400 ? n : s->r[n];
        s->r[insn & 7] = insn & 0x200 ? add_flags(s, a, ~b, 1, 1) : add_flags(s, a, b, 0, 1);
        return;
    }
    case 4: s->r[insn >> 8 & 7] = insn & 0xFF; set_nz(s, insn & 0xFF); return;          /* MOV */
    case 5: add_flags(s, s->r[insn >> 8 & 7], ~(insn & 0xFF), 1, 1); return;            /* CMP */
    case 6: s->r[insn >> 8 & 7] = add_flags(s, s->r[insn >> 8 & 7], insn & 0xFF, 0, 1); return;
    case 7: s->r[insn >> 8 & 7] = add_flags(s, s->r[insn >> 8 & 7], ~(insn & 0xFF), 1, 1); return;
    case 8:
        if (!(insn & 0x400)) { thumb_alu(s, insn); return; }
        {
            unsigned rd = (insn & 7) | (insn >> 4 & 8), rm = insn >> 3 & 15;
            switch (insn >> 8 & 3) {
            case 0: {
                uint32_t r = TV(rd) + TV(rm);
                if (rd == 15) s->next_pc = r & ~1u; else s->r[rd] = r;
                return;
            }
            case 1: add_flags(s, TV(rd), ~TV(rm), 1, 1); return;
            case 2:
                if (rd == 15) s->next_pc = TV(rm) & ~1u; else s->r[rd] = TV(rm);
                return;
            default: {
                uint32_t t = TV(rm);
                if (insn & 0x80) s->r[14] = (pc + 2) | 1;     /* BLX register */
                bx(s, t);
                return;
            }
            }
        }
    case 9:                                                   /* LDR literal */
        s->r[insn >> 8 & 7] = mem_load(s, ((pc + 4) & ~3u) + (insn & 0xFF) * 4, 4);
        return;
    case 10: case 11: {                                       /* register offset */
        uint32_t a = s->r[insn >> 3 & 7] + s->r[insn >> 6 & 7];
        unsigned rd = insn & 7;
        switch (insn >> 9 & 7) {
        case 0: mem_store(s, a, 4, s->r[rd]); return;
        case 1: mem_store(s, a, 2, s->r[rd]); return;
        case 2: mem_store(s, a, 1, s->r[rd] & 0xFF); return;
        case 3: s->r[rd] = (uint32_t)(int32_t)(int8_t)mem_load(s, a, 1); return;
        case 4: s->r[rd] = mem_load(s, a, 4); return;
        case 5: s->r[rd] = mem_load(s, a, 2); return;
        case 6: s->r[rd] = mem_load(s, a, 1); return;
        default: s->r[rd] = (uint32_t)(int32_t)(int16_t)mem_load(s, a, 2); return;
        }
    }
    case 12: case 13: case 14: case 15: {                     /* immediate offset, word / byte */
        int byte = insn & 0x1000;
        uint32_t a = s->r[insn >> 3 & 7] + ((insn >> 6 & 31) << (byte ? 0 : 2));
        if (insn & 0x800) s->r[insn & 7] = mem_load(s, a, byte ? 1 : 4);
        else mem_store(s, a, byte ? 1 : 4, byte ? s->r[insn & 7] & 0xFF : s->r[insn & 7]);
        return;
    }
    case 16: case 17: {                                       /* halfword, immediate offset */
        uint32_t a = s->r[insn >> 3 & 7] + ((insn >> 6 & 31) << 1);
        if (insn & 0x800) s->r[insn & 7] = mem_load(s, a, 2);
        else mem_store(s, a, 2, s->r[insn & 7]);
        return;
    }
    case 18: case 19: {                                       /* SP-relative */
        uint32_t a = s->r[13] + (insn & 0xFF) * 4;
        if (insn & 0x800) s->r[insn >> 8 & 7] = mem_load(s, a, 4);
        else mem_store(s, a, 4, s->r[insn >> 8 & 7]);
        return;
    }
    case 20: s->r[insn >> 8 & 7] = ((pc + 4) & ~3u) + (insn & 0xFF) * 4; return;   /* ADR */
    case 21: s->r[insn >> 8 & 7] = s->r[13] + (insn & 0xFF) * 4; return;
    case 22: case 23:
        if ((insn & 0xFF00) == 0xB000) {                      /* ADD / SUB SP */
            uint32_t k = (insn & 0x7F) * 4;
            s->r[13] = insn & 0x80 ? s->r[13] - k : s->r[13] + k;
        } else if ((insn & 0x0600) == 0x0400) {               /* PUSH / POP */
            unsigned list = insn & 0xFF, n = __builtin_popcount(list) + (insn >> 8 & 1);
            if (insn & 0x800) {
                uint32_t a = s->r[13];
                s->r[13] = a + 4 * n;
                for (unsigned i = 0; i < 8; i++)
                    if (list >> i & 1) { s->r[i] = mem_load(s, a, 4); a += 4; }
                if (insn & 0x100) bx(s, mem_load(s, a, 4));
            } else {
                uint32_t a = s->r[13] - 4 * n;
                s->r[13] = a;
                for (unsigned i = 0; i < 8; i++)
                    if (list >> i & 1) { mem_store(s, a, 4, s->r[i]); a += 4; }
                if (insn & 0x100) mem_store(s, a, 4, s->r[14]);
            }
        } else if ((insn & 0xFF00) == 0xBE00) {
            s->intr = 7;                                      /* BKPT */
        } else {
            undefined(s);
        }
        return;
    case 24: case 25: {                                       /* STMIA / LDMIA rb! */
        unsigned rb = insn >> 8 & 7, list = insn & 0xFF;
        uint32_t a = s->r[rb], final = a + 4 * __builtin_popcount(list);
        if (insn & 0x800) {
            s->r[rb] = final;
            for (unsigned i = 0; i < 8; i++)
                if (list >> i & 1) { s->r[i] = mem_load(s, a, 4); a += 4; }
        } else {
            for (unsigned i = 0; i < 8; i++)
                if (list >> i & 1) { mem_store(s, a, 4, s->r[i]); a += 4; }
            s->r[rb] = final;
        }
        return;
    }
    case 26: case 27: {                                       /* conditional branch, SWI */
        unsigned cond = insn >> 8 & 15;
        if (cond == 15) { s->intr = 2; return; }
        if (cond == 14) { undefined(s); return; }
        if (cond_ok(s->cpsr, cond)) s->next_pc = pc + 4 + ((int32_t)(insn << 24) >> 23);
        return;
    }
    case 28: s->next_pc = pc + 4 + ((int32_t)(insn << 21) >> 20); return;          /* B */
    case 29: {                                                /* BLX suffix: to ARM */
        if (insn & 1) { undefined(s); return; }
        uint32_t t = (s->r[14] + ((insn & 0x7FF) << 1)) & ~3u;
        s->r[14] = (pc + 2) | 1;
        s->cpsr &= ~T_BIT;
        s->next_pc = t;
        return;
    }
    case 30: s->r[14] = pc + 4 + ((int32_t)(insn << 21) >> 9); return;             /* BL prefix */
    default: {                                                /* BL suffix */
        uint32_t t = s->r[14] + ((insn & 0x7FF) << 1);
        s->r[14] = (pc + 2) | 1;
        s->next_pc = t & ~1u;
        return;
    }
    }
}
#undef TV

/* ---- run loop ----------------------------------------------------------------------- */
static void call_code_hooks(arm_t *s, uint32_t pc) {
    for (int i = 0; i < s->nhooks; i++) {
        const hook_t *h = &s->hooks[i];
        if (h->live && h->kind == HK_CODE && (h->begin > h->end || (pc >= h->begin && pc <= h->end))) {
            if (h->fn) h->fn(h->ctx, 0, pc, s->cpsr & T_BIT ? 2 : 4, 0);
            else s->cb(s->host, i, 0, pc, s->cpsr & T_BIT ? 2 : 4, 0);
        }
    }
}

static void raise_interrupt(arm_t *s, int intno) {
    if (!s->has_intr) { s->err = ERR_EXCEPTION; s->stop = 1; return; }
    for (int i = 0; i < s->nhooks; i++)
        if (s->hooks[i].live && s->hooks[i].kind == HK_INTR) {
            if (s->hooks[i].fn) s->hooks[i].fn(s->hooks[i].ctx, 0, (uint32_t)intno, 0, 0);
            else s->cb(s->host, i, 0, (uint32_t)intno, 0, 0);
        }
}

int arm_run(arm_t *s, uint32_t begin, uint32_t until, uint64_t count) {
    arm_reg_write(s, 15, begin);
    s->stop = 0;
    s->err = ERR_OK;
    for (uint64_t done = 0; !s->stop && (!count || done < count); done++) {
        uint32_t pc = s->r[15];
        if (pc == until)
            break;
        if (s->pflags[pc >> PAGE_BITS] & PF_HCODE) {
            call_code_hooks(s, pc);
            if (s->stop) break;
            if (s->r[15] != pc) continue;                     /* the hook moved PC */
        }
        int thumb = s->cpsr & T_BIT;
        int size = thumb ? 2 : 4;
        uint32_t insn;
        uint8_t *p = s->page[pc >> PAGE_BITS];
        if (__builtin_expect(p != 0, 1))
            insn = be_get(p + (pc & (PAGE_SIZE - 1)), size);
        else if (!mem_fetch_slow(s, pc, size, &insn))
            break;
        s->next_pc = pc + size;
        s->intr = -1;
        if (thumb && insn >> 11 == 30 && (pc & 0x3FF) < 0x3FD) {
            /* A BL / BLX prefix with its suffix on the same 1 KB page (QEMU's page size for
               this core) runs as one instruction, as in Unicorn, so instruction counts (and
               so slice ends) agree. */
            uint32_t suffix = be_get(s->page[pc >> PAGE_BITS] + ((pc + 2) & (PAGE_SIZE - 1)), 2);
            exec_thumb(s, insn, pc);
            if (suffix >> 11 == 31 || suffix >> 11 == 29) {
                s->next_pc = pc + 4;
                exec_thumb(s, suffix, pc + 2);
            }
        } else if (thumb) {
            exec_thumb(s, insn, pc);
        } else {
            exec_arm(s, insn, pc);
        }
        s->executed++;
        if (s->err) break;
        if (s->intr >= 0) {                                   /* PC stays on the instruction */
            raise_interrupt(s, s->intr);
            continue;
        }
        s->r[15] = s->next_pc;
    }
    return s->err;
}
