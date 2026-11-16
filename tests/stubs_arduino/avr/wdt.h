/*
 * Stub mínimo de <avr/wdt.h> (avr-libc) para checar a sintaxe do firmware com
 * g++ -D__AVR__ sem a toolchain AVR.
 */
#ifndef AGROANALISE_STUB_AVR_WDT_H
#define AGROANALISE_STUB_AVR_WDT_H

#include <stdint.h>

#define WDTO_8S 9

extern volatile uint8_t MCUSR;
void wdt_enable(uint8_t prazo);
void wdt_disable();
void wdt_reset();

#endif
