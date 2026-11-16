/*
 * Stub mínimo do núcleo Arduino para checar a SINTAXE do firmware com g++
 * (sem toolchain AVR):
 *     g++ -fsyntax-only -x c++ -I tests/stubs_arduino arduino/coleta_sensores.ino
 * Só declara o que o firmware usa; não executa nada no hardware.
 */
#ifndef AGROANALISE_STUB_ARDUINO_H
#define AGROANALISE_STUB_ARDUINO_H

#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <math.h>
#include <stdio.h>

#define HIGH 0x1
#define LOW 0x0
#define INPUT 0x0
#define OUTPUT 0x1
#define LED_BUILTIN 13
#define A0 14

#ifndef NAN
#define NAN (__builtin_nanf(""))
#endif

class __FlashStringHelper;
#define F(texto) (reinterpret_cast<const __FlashStringHelper*>(texto))

template <typename T>
static inline T constrain(T valor, T minimo, T maximo) {
  return valor < minimo ? minimo : (valor > maximo ? maximo : valor);
}

unsigned long millis();
void delay(unsigned long milissegundos);
int analogRead(uint8_t pino);
void digitalWrite(uint8_t pino, uint8_t valor);
void pinMode(uint8_t pino, uint8_t modo);
char* dtostrf(double valor, signed char largura, unsigned char casas, char* destino);

class HardwareSerial {
 public:
  void begin(unsigned long velocidade);
  size_t print(const char* texto);
  size_t print(char caractere);
  size_t print(double valor, int casas = 2);
  size_t print(const __FlashStringHelper* texto);
  size_t println(const char* texto);
  size_t println(char caractere);
  size_t println(double valor, int casas = 2);
  size_t println(const __FlashStringHelper* texto);
  size_t println();
};

extern HardwareSerial Serial;

#endif
