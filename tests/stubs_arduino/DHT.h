/*
 * Stub mínimo da biblioteca "DHT sensor library" (Adafruit) para checar a
 * sintaxe do firmware com g++. Só as declarações usadas pelo firmware.
 */
#ifndef AGROANALISE_STUB_DHT_H
#define AGROANALISE_STUB_DHT_H

#include <stdint.h>

#define DHT11 11
#define DHT22 22

class DHT {
 public:
  DHT(uint8_t pino, uint8_t tipo);
  void begin();
  float readTemperature(bool fahrenheit = false);
  float readHumidity();
};

#endif
