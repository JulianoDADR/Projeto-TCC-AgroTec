/*
 * Executa o firmware NO COMPUTADOR (sem placa) para conferir o protocolo 2:
 *     g++ -I tests/stubs_arduino -I arduino -o fw tests/stubs_arduino/execucao_host.cpp && ./fw
 * Implementa os stubs de Arduino.h/DHT.h com um relógio simulado, roda o
 * setup() e ~4,5 s de loop() e imprime na saída padrão o que o firmware
 * mandaria pela serial. A partir de 2,5 s o DHT11 passa a falhar (NaN), para
 * conferir a linha "$2,nan,nan,<solo>*XX". O teste em Python interpreta a
 * saída com o mesmo código do coletor (checksum incluso).
 *
 * Alimentação pulsada da sonda (-DAGRO_PINO_ALIMENTACAO_SOLO=7): enquanto o
 * pino da sonda está em HIGH o relógio simulado anda 1 ms a cada millis()
 * (a espera de 20 ms do firmware termina), e cada analogRead() é contado como
 * "sonda ligada" ou "sonda desligada". O resumo vai para a SAÍDA DE ERRO
 * (a saída padrão continua sendo só o que iria pela serial):
 *     #HOST:LEITURAS_SOLO_LIGADA=<n>;LEITURAS_SOLO_DESLIGADA=<m>;PULSOS=<p>
 */
#include <stdio.h>

#include "Arduino.h"
#include "DHT.h"

static unsigned long relogioSimulado = 0;
static bool dhtComFalha = false;
static bool sondaLigada = false;
static unsigned long leiturasSondaLigada = 0;
static unsigned long leiturasSondaDesligada = 0;
static unsigned long pulsosSonda = 0;

HardwareSerial Serial;

unsigned long millis() {
  if (sondaLigada) {
    relogioSimulado += 1;  // o tempo passa enquanto o firmware espera a sonda estabilizar
  }
  return relogioSimulado;
}
void delay(unsigned long milissegundos) { relogioSimulado += milissegundos; }
int analogRead(uint8_t) {
  if (sondaLigada) {
    leiturasSondaLigada++;
  } else {
    leiturasSondaDesligada++;
  }
  return 500;
}
void digitalWrite(uint8_t pino, uint8_t valor) {
  if (pino == LED_BUILTIN) {
    return;
  }
  if (valor == HIGH && !sondaLigada) {
    pulsosSonda++;
  }
  sondaLigada = (valor == HIGH);
}
void pinMode(uint8_t, uint8_t) {}
char* dtostrf(double valor, signed char largura, unsigned char casas, char* destino) {
  sprintf(destino, "%*.*f", largura, casas, valor);
  return destino;
}

void HardwareSerial::begin(unsigned long) {}
size_t HardwareSerial::print(const char* texto) { return (size_t)printf("%s", texto); }
size_t HardwareSerial::print(char caractere) { return (size_t)printf("%c", caractere); }
size_t HardwareSerial::print(double valor, int casas) { return (size_t)printf("%.*f", casas, valor); }
size_t HardwareSerial::print(const __FlashStringHelper* texto) {
  return (size_t)printf("%s", reinterpret_cast<const char*>(texto));
}
size_t HardwareSerial::println(const char* texto) { return (size_t)printf("%s\r\n", texto); }
size_t HardwareSerial::println(char caractere) { return (size_t)printf("%c\r\n", caractere); }
size_t HardwareSerial::println(double valor, int casas) { return (size_t)printf("%.*f\r\n", casas, valor); }
size_t HardwareSerial::println(const __FlashStringHelper* texto) {
  return (size_t)printf("%s\r\n", reinterpret_cast<const char*>(texto));
}
size_t HardwareSerial::println() { return (size_t)printf("\r\n"); }

DHT::DHT(uint8_t, uint8_t) {}
void DHT::begin() {}
float DHT::readTemperature(bool) { return dhtComFalha ? NAN : 21.0f; }
float DHT::readHumidity() { return dhtComFalha ? NAN : 65.0f; }

#include "coleta_sensores.ino"

int main() {
  setup();
  for (int passo = 0; passo < 4500; passo++) {
    if (passo == 2500) {
      dhtComFalha = true;
    }
    loop();
    relogioSimulado += 1;
  }
  fprintf(stderr, "#HOST:LEITURAS_SOLO_LIGADA=%lu;LEITURAS_SOLO_DESLIGADA=%lu;PULSOS=%lu\n",
          leiturasSondaLigada, leiturasSondaDesligada, pulsosSonda);
  return 0;
}
