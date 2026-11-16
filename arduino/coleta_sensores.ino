/*
 * =============================================================================
 *  AgroAnálise — Módulo 1: Firmware IoT de coleta de sensores
 *  Arquivo: arduino/coleta_sensores.ino
 * =============================================================================
 *
 *  O que este firmware faz
 *  -----------------------
 *  1. Lê a temperatura e a umidade do ar com o sensor DHT11 (padrão) ou DHT22
 *     (pino digital D2), escolhido por #define AGRO_SENSOR_AR (ver abaixo).
 *  2. Lê a umidade do solo com um higrômetro resistivo analógico (pino A0),
 *     opcionalmente com ALIMENTAÇÃO PULSADA (sonda energizada só durante a
 *     medida, contra a corrosão por eletrólise — #define AGRO_PINO_ALIMENTACAO_SOLO).
 *  3. Estabiliza a leitura do solo com uma MÉDIA MÓVEL de N = 10 amostras,
 *     guardadas num buffer circular e amostradas a cada 100 ms SEM travar o
 *     microcontrolador (tudo baseado em millis(), nunca em delay() no loop).
 *     Com alimentação pulsada, 1 amostra (média de 10 leituras do ADC) por
 *     envio de 2 s: a média móvel passa a cobrir 20 s.
 *  4. A cada 2000 ms envia pela porta Serial (9600 bps) uma linha do
 *     PROTOCOLO 2, com checksum (estilo NMEA):
 *
 *         $2,temperatura_ar,umidade_ar,umidade_solo*XX   (ex.: $2,21.0,65.0,72.4*01)
 *
 *     sempre com 1 casa decimal e terminada em "\r\n". XX é o XOR, em
 *     hexadecimal MAIÚSCULO de 2 dígitos, de todos os bytes ENTRE '$' e '*'.
 *     Ruído no cabo USB longo que troque um dígito ("21.0" -> "71.0") muda o
 *     XOR e a linha é descartada pelo coletor, em vez de virar alarme falso.
 *     Se o DHT11 falhar, a linha sai mesmo assim, com "nan" no ar:
 *         $2,nan,nan,72.4*XX
 *     (a umidade do solo continua sendo medida e enviada).
 *     O coletor também aceita o protocolo 1 antigo (CSV puro "21.0,65.0,72.4").
 *  5. Mensagens de diagnóstico começam com '#', para que o coletor Python
 *     (src/coletor_serial.py) as registre em log e as ignore como dado:
 *         #AGROANALISE_PRONTO;PROTOCOLO=2     -> placa iniciou corretamente
 *         #AGROANALISE_PERFIL;PROTOCOLO=2;AR=DHT11;SOLO=RESISTIVO;PULSO=0
 *                                             -> perfil dos sensores (logo após o
 *                                                banner; PULSO=1 com alimentação
 *                                                pulsada). O coletor grava o sensor
 *                                                de ar e a sua incerteza (±2 °C no
 *                                                DHT11, ±0,5 °C no DHT22), usada no
 *                                                limiar de geada. O banner fica
 *                                                idêntico ao da v2 para que um
 *                                                coletor v2 continue funcionando.
 *         #ERRO:DHT11_LEITURA_INVALIDA        -> DHT devolveu NaN (isnan); "DHT22_" com DHT22
 *         #AVISO:DHT11_REINICIADO             -> 5 falhas seguidas: dht.begin() de novo
 *         #AVISO:HIGROMETRO_CURTO_CIRCUITO    -> média do ADC < 5 (pontas em curto)
 *         #AVISO:HIGROMETRO_DESCONECTADO      -> ADC parado em 1023 por muito tempo
 *         #AVISO:HIGROMETRO_FORA_DA_FAIXA     -> leitura além da calibração "encharcado"
 *  6. Pisca o LED embutido (LED_BUILTIN) a cada linha de dados enviada
 *     (heartbeat). Se o LED parar de piscar, a placa não está enviando dados
 *     válidos — um sinal simples para quem está no campo.
 *  7. WATCHDOG (cão de guarda) de 8 s: se o programa travar (ruído elétrico,
 *     biblioteca presa esperando o sensor), o próprio microcontrolador se
 *     reinicia sozinho e volta a mandar dados, sem ninguém ir ao campo.
 *     Desligue com "#define AGRO_USAR_WATCHDOG 0" se a sua placa tiver um
 *     bootloader antigo que entra em laço de reinício com o watchdog.
 *
 *  Biblioteca necessária (Arduino IDE -> Gerenciar Bibliotecas):
 *     "DHT sensor library" (Adafruit) + dependência "Adafruit Unified Sensor".
 *
 * =============================================================================
 *  LIGAÇÃO ELÉTRICA (Arduino Uno / Nano — 5 V)
 * =============================================================================
 *
 *  Sensor DHT11 (temperatura e umidade do ar)
 *  ------------------------------------------
 *     DHT11 VCC  (pino 1) ---> 5V do Arduino
 *     DHT11 DATA (pino 2) ---> D2 do Arduino
 *     DHT11 NC   (pino 3) ---> não conectar (só existe no sensor "nu", 4 pinos)
 *     DHT11 GND  (pino 4) ---> GND do Arduino
 *
 *     Resistor de PULL-UP de 10 kOhm entre DATA e VCC:
 *
 *          5V ----+-------------- VCC (DHT11)
 *                 |
 *               [10k]
 *                 |
 *          D2 ----+-------------- DATA (DHT11)
 *
 *     O DHT11 usa um protocolo de um fio só; a linha DATA precisa "descansar"
 *     em nível ALTO. Sem o pull-up a leitura falha aleatoriamente e a
 *     biblioteca devolve NaN. Os módulos DHT11 em plaquinha azul (3 pinos)
 *     normalmente JÁ trazem esse resistor soldado — confira antes de pôr outro.
 *     Mantenha o cabo DATA curto (até ~20 m com pull-up adequado; quanto menor,
 *     melhor) e proteja o sensor do sol direto e da chuva (abrigo ventilado).
 *
 *     INSTALAÇÃO DO SENSOR DE AR (obrigatória para o aviso de geada valer):
 *     dentro de um ABRIGO ventilado e branco, a 1,5 m do chão, longe de
 *     paredes e telhados. O limiar de geada do coletor (2 °C no abrigo, Epamig/
 *     Embrapa Café) vale para essa altura: na folha, numa noite de geada, a
 *     temperatura fica alguns graus abaixo da do abrigo.
 *
 *     DHT11 x DHT22 (datasheets): o DHT11 mede de 0 a 50 °C com erro de ±2 °C
 *     e NÃO mede abaixo de 0 °C (https://ece-classes.usc.edu/ee459/library/datasheets/DHT11.pdf);
 *     o DHT22/AM2302 mede de -40 a 80 °C com ±0,5 °C, no mesmo pino e com a
 *     mesma biblioteca. Para geada, prefira o DHT22: compile com
 *         #define AGRO_SENSOR_AR AGRO_AR_DHT22
 *
 *  Higrômetro resistivo de solo (sonda em "garfo" + módulo comparador)
 *  -------------------------------------------------------------------
 *     Módulo VCC ---> 5V do Arduino
 *     Módulo GND ---> GND do Arduino
 *     Módulo A0  ---> A0 do Arduino   (saída ANALÓGICA — é a que usamos)
 *     Módulo D0  ---> não conectar    (saída digital do comparador, liga/desliga
 *                                      por limiar do trimpot; não serve para %)
 *     Sonda (garfo) ---> 2 fios até o módulo (sem polaridade).
 *
 *     Como funciona: a água conduz eletricidade. Solo SECO = alta resistência
 *     = tensão alta na saída A0 = ADC perto de 1023. Solo ENCHARCADO = baixa
 *     resistência = tensão baixa = ADC perto de 300. Ou seja, a escala é
 *     INVERTIDA e o firmware converte para 0 % (seco) .. 100 % (encharcado).
 *
 *     ALIMENTAÇÃO PULSADA (contra a eletrólise): com VCC ligado direto no 5V a
 *     sonda fica energizada 24 h por dia e as pontas metálicas se corroem
 *     (eletrólise) em poucas semanas, deslocando a calibração. Para aumentar a
 *     vida útil, ligue o VCC do MÓDULO a um PINO DIGITAL (ex.: D7; consumo
 *     ~5 mA, dentro do limite de 20 mA do pino) e compile com
 *         #define AGRO_PINO_ALIMENTACAO_SOLO 7
 *     O firmware então liga a sonda, espera 20 ms (sem travar o watchdog),
 *     lê 10 amostras, tira a média e desliga — UMA vez por envio (2 s): a
 *     sonda fica energizada ~1 % do tempo. É a mesma estratégia do sensor
 *     aberto b-parasite (https://github.com/rbaron/b-parasite), que só
 *     alimenta o circuito de medida durante a leitura. Outra opção é trocar
 *     por um sensor CAPACITIVO (v1.2/v2.0), sem metal exposto. Em qualquer
 *     caso, calibre CADA sonda no solo da lavoura (a calibração por sonda é
 *     aplicada no computador; ver https://www.infoteca.cnptia.embrapa.br/infoteca/bitstream/doc/1170710/1/Protocolo-para-a-calibracao-e-a-afericao-de-sensores-de-umidade-de-solo.pdf).
 *
 * =============================================================================
 *  CALIBRAÇÃO DO HIGRÔMETRO (faça uma vez por sonda / tipo de solo)
 * =============================================================================
 *     1. Abra o Monitor Serial (9600 bps). Deixe a sonda NO AR, limpa e seca,
 *        e anote o valor bruto (normalmente ~1023)   -> ADC_SOLO_SECO.
 *        Para ver o valor bruto, troque temporariamente o envio do campo
 *        umidade_solo por mediaAdcSolo() ou use o exemplo "AnalogReadSerial".
 *     2. Mergulhe a sonda num copo com o solo da lavoura bem encharcado (ou
 *        água, até a marca da sonda) e anote o valor (~300) -> ADC_SOLO_ENCHARCADO.
 *     3. Atualize as duas constantes abaixo e grave o firmware de novo.
 *     A conversão é linear:
 *        umidade_solo(%) = (ADC_SOLO_SECO - média) * 100 / (ADC_SOLO_SECO - ADC_SOLO_ENCHARCADO)
 *     e o resultado é limitado (constrain) ao intervalo 0..100 %.
 * =============================================================================
 */

#include <Arduino.h>
#include <DHT.h>

// -----------------------------------------------------------------------------
// Watchdog (reinício automático da placa se o loop travar por mais de 8 s)
// -----------------------------------------------------------------------------
#ifndef AGRO_USAR_WATCHDOG
#define AGRO_USAR_WATCHDOG 1
#endif

#if defined(__AVR__) && AGRO_USAR_WATCHDOG
#include <avr/wdt.h>
#endif

// -----------------------------------------------------------------------------
// Protocolo serial
// -----------------------------------------------------------------------------
#define VERSAO_PROTOCOLO "2"
const uint8_t TAMANHO_LINHA_PROTOCOLO = 48;         // "2,-99.9,100.0,100.0" cabe com folga
const uint8_t TAMANHO_CAMPO_NUMERICO = 12;          // um valor com 1 casa decimal

// -----------------------------------------------------------------------------
// Pinos e tipo de sensor (definidos pelo contrato técnico do projeto)
// -----------------------------------------------------------------------------
#define AGRO_AR_DHT11 11
#define AGRO_AR_DHT22 22
#ifndef AGRO_SENSOR_AR
#define AGRO_SENSOR_AR AGRO_AR_DHT11       // padrão: DHT11 (0–50 °C, ±2 °C)
#endif

#if AGRO_SENSOR_AR == AGRO_AR_DHT22
#define TIPO_DHT DHT22
#define NOME_SENSOR_AR "DHT22"
#elif AGRO_SENSOR_AR == AGRO_AR_DHT11
#define TIPO_DHT DHT11
#define NOME_SENSOR_AR "DHT11"
#else
#error "AGRO_SENSOR_AR deve ser AGRO_AR_DHT11 ou AGRO_AR_DHT22"
#endif

#define PINO_DHT 2
#define PINO_HIGROMETRO A0

// Pino digital que ALIMENTA o módulo do higrômetro (alimentação pulsada).
// -1 (padrão) = módulo ligado direto no 5V, comportamento da v2.
#ifndef AGRO_PINO_ALIMENTACAO_SOLO
#define AGRO_PINO_ALIMENTACAO_SOLO -1
#endif

#if AGRO_PINO_ALIMENTACAO_SOLO >= 0
#define TEXTO_PULSO "1"
#else
#define TEXTO_PULSO "0"
#endif

// -----------------------------------------------------------------------------
// Calibração do higrômetro (ver instruções no cabeçalho)
// -----------------------------------------------------------------------------
const int ADC_SOLO_SECO = 1023;       // leitura com a sonda no ar / solo totalmente seco
const int ADC_SOLO_ENCHARCADO = 300;  // leitura com a sonda em solo saturado de água

// -----------------------------------------------------------------------------
// Temporização (todas em milissegundos, comparadas com millis())
// -----------------------------------------------------------------------------
const uint32_t INTERVALO_AMOSTRA_SOLO_MS = 100UL;   // período de amostragem do higrômetro
const uint32_t INTERVALO_ENVIO_MS = 2000UL;         // DHT11 exige >= 1 s entre leituras; usamos 2 s
const uint32_t DURACAO_PULSO_LED_MS = 100UL;        // tempo que o LED fica aceso no heartbeat
const uint32_t ESPERA_PREENCHIMENTO_MS = 10UL;      // pausa entre amostras no pré-preenchimento (só no setup)
const uint32_t ESPERA_ESTABILIZACAO_SONDA_MS = 20UL; // sonda pulsada: espera após ligar, antes de ler
const uint8_t AMOSTRAS_POR_PULSO = 10;              // sonda pulsada: amostras do ADC por pulso

// -----------------------------------------------------------------------------
// Filtro de média móvel e diagnóstico
// -----------------------------------------------------------------------------
const uint8_t TAMANHO_JANELA_MEDIA = 10;            // N = 10 amostras na média móvel
const float LIMIAR_CURTO_CIRCUITO_ADC = 5.0f;       // média abaixo disso = pontas em curto
const int ADC_MAXIMO = 1023;                        // valor máximo do conversor de 10 bits
// Amostras consecutivas cravadas em 1023 para considerar sonda desconectada:
// 60 s em ambos os modos (600 x 100 ms contínuo; 30 x 2 s pulsado). Solo
// realmente seco raramente fica EXATAMENTE em 1023 por um minuto inteiro
// (sempre há algum ruído), mas um fio solto fica.
#if AGRO_PINO_ALIMENTACAO_SOLO >= 0
const uint16_t AMOSTRAS_CONSTANTES_DESCONEXAO = 30;
#else
const uint16_t AMOSTRAS_CONSTANTES_DESCONEXAO = 600;
#endif
// Margem abaixo de ADC_SOLO_ENCHARCADO tolerada antes de avisar "fora da faixa"
// (indica água pura/salina na sonda ou necessidade de recalibrar).
const int MARGEM_FORA_DA_FAIXA_ADC = 50;
const uint8_t MAXIMO_FALHAS_DHT_SEGUIDAS = 5;       // após 5 NaN seguidos, reinicia o DHT

// -----------------------------------------------------------------------------
// Estado global
// -----------------------------------------------------------------------------
DHT dht(PINO_DHT, TIPO_DHT);

uint16_t bufferSolo[TAMANHO_JANELA_MEDIA];   // buffer circular das últimas N leituras do ADC
uint8_t indiceBufferSolo = 0;                // posição que será sobrescrita na próxima amostra
uint32_t somaBufferSolo = 0;                 // soma acumulada (evita somar o vetor inteiro a cada vez)
uint16_t amostrasSeguidasEmMaximo = 0;       // contador de leituras brutas consecutivas em 1023

uint8_t falhasDhtSeguidas = 0;               // leituras NaN consecutivas do DHT11

uint32_t instanteUltimaAmostraSolo = 0;
uint32_t instanteUltimoEnvio = 0;
uint32_t instanteLedAceso = 0;
bool ledAceso = false;

// -----------------------------------------------------------------------------
// Protótipos (a IDE do Arduino os gera sozinha; declaramos para ficar explícito
// e permitir compilar o arquivo também como C++ comum)
// -----------------------------------------------------------------------------
void preencherBufferSolo();
uint16_t lerSoloComPulso();
void registrarAmostraSolo(uint16_t leitura);
void amostrarHigrometro();
float mediaAdcSolo();
float converterAdcParaPercentual(float mediaAdc);
void verificarHigrometro(float mediaAdc);
void lerSensoresEEnviar();
void tratarFalhaDht();
void acenderLedHeartbeat(uint32_t agora);
void atualizarLedHeartbeat(uint32_t agora);
void anexarTexto(char* destino, uint8_t tamanho, const char* texto);
void anexarValor(char* destino, uint8_t tamanho, float valor);
uint8_t calcularChecksum(const char* conteudo);
void enviarLinhaProtocolo(float temperaturaAr, float umidadeAr, float umidadeSolo);

/**
 * Lê o higrômetro UMA vez. Com AGRO_PINO_ALIMENTACAO_SOLO >= 0 (alimentação
 * pulsada): liga a sonda (HIGH no pino), espera ESPERA_ESTABILIZACAO_SONDA_MS
 * comparando millis() — e alimentando o watchdog de 8 s enquanto espera —,
 * lê AMOSTRAS_POR_PULSO amostras do ADC, calcula a média arredondada e
 * desliga a sonda (LOW). Sem pulso: um analogRead() simples, como na v2.
 */
uint16_t lerSoloComPulso() {
#if AGRO_PINO_ALIMENTACAO_SOLO >= 0
  digitalWrite(AGRO_PINO_ALIMENTACAO_SOLO, HIGH);
  const uint32_t inicio = millis();
  while ((uint32_t)(millis() - inicio) < ESPERA_ESTABILIZACAO_SONDA_MS) {
#if defined(__AVR__) && AGRO_USAR_WATCHDOG
    wdt_reset();
#endif
  }
  uint32_t soma = 0;
  for (uint8_t i = 0; i < AMOSTRAS_POR_PULSO; i++) {
    soma += (uint16_t)analogRead(PINO_HIGROMETRO);
  }
  digitalWrite(AGRO_PINO_ALIMENTACAO_SOLO, LOW);
  return (uint16_t)((soma + AMOSTRAS_POR_PULSO / 2) / AMOSTRAS_POR_PULSO);
#else
  return (uint16_t)analogRead(PINO_HIGROMETRO);
#endif
}

/**
 * Lê o higrômetro N vezes e preenche o buffer circular inteiro, para que
 * a primeira média enviada já seja representativa (sem "rampa" a partir de zero).
 * Usa delay() curto porque roda apenas uma vez, dentro do setup().
 */
void preencherBufferSolo() {
  somaBufferSolo = 0;
  amostrasSeguidasEmMaximo = 0;
  for (uint8_t i = 0; i < TAMANHO_JANELA_MEDIA; i++) {
    const uint16_t leitura = lerSoloComPulso();
    bufferSolo[i] = leitura;
    somaBufferSolo += leitura;
    if (leitura >= ADC_MAXIMO) {
      amostrasSeguidasEmMaximo++;
    } else {
      amostrasSeguidasEmMaximo = 0;
    }
    delay(ESPERA_PREENCHIMENTO_MS);
  }
  indiceBufferSolo = 0;
}

/**
 * Faz UMA amostra do higrômetro (pulsada ou contínua) e a registra na média móvel.
 */
void amostrarHigrometro() {
  registrarAmostraSolo(lerSoloComPulso());
}

/**
 * Atualiza a média móvel em O(1) com uma amostra: retira da soma o valor
 * mais antigo, grava o novo no mesmo lugar e soma-o. Também conta quantas
 * leituras seguidas ficaram cravadas em 1023.
 */
void registrarAmostraSolo(uint16_t leitura) {
  somaBufferSolo -= bufferSolo[indiceBufferSolo];
  bufferSolo[indiceBufferSolo] = leitura;
  somaBufferSolo += leitura;
  indiceBufferSolo = (uint8_t)((indiceBufferSolo + 1) % TAMANHO_JANELA_MEDIA);

  if (leitura >= ADC_MAXIMO) {
    // Satura o contador para não estourar o uint16_t em desconexões longas.
    if (amostrasSeguidasEmMaximo < AMOSTRAS_CONSTANTES_DESCONEXAO) {
      amostrasSeguidasEmMaximo++;
    }
  } else {
    amostrasSeguidasEmMaximo = 0;
  }
}

/**
 * Retorna a média móvel atual do ADC do higrômetro (0..1023) como float.
 */
float mediaAdcSolo() {
  return (float)somaBufferSolo / (float)TAMANHO_JANELA_MEDIA;
}

/**
 * Converte a média do ADC em umidade do solo (%) usando a calibração linear
 * ADC_SOLO_SECO -> 0 % e ADC_SOLO_ENCHARCADO -> 100 %, limitada a 0..100.
 * Usa aritmética em float (map() do Arduino trabalha só com inteiros e
 * perderia a casa decimal).
 */
float converterAdcParaPercentual(float mediaAdc) {
  const float faixa = (float)(ADC_SOLO_SECO - ADC_SOLO_ENCHARCADO);
  const float percentual = ((float)ADC_SOLO_SECO - mediaAdc) * 100.0f / faixa;
  return constrain(percentual, 0.0f, 100.0f);
}

/**
 * Emite avisos de diagnóstico sobre o higrômetro. Os avisos NÃO impedem o
 * envio da linha de dados: o coletor Python e o saneamento decidem o que
 * fazer com valores suspeitos.
 */
void verificarHigrometro(float mediaAdc) {
  if (mediaAdc < LIMIAR_CURTO_CIRCUITO_ADC) {
    Serial.println(F("#AVISO:HIGROMETRO_CURTO_CIRCUITO"));
  } else if (amostrasSeguidasEmMaximo >= AMOSTRAS_CONSTANTES_DESCONEXAO) {
    Serial.println(F("#AVISO:HIGROMETRO_DESCONECTADO"));
  } else if (mediaAdc < (float)(ADC_SOLO_ENCHARCADO - MARGEM_FORA_DA_FAIXA_ADC)) {
    Serial.println(F("#AVISO:HIGROMETRO_FORA_DA_FAIXA"));
  }
}

/**
 * Acrescenta "texto" ao fim de "destino" sem ultrapassar "tamanho" bytes
 * (incluindo o '\0' final). O que não couber é cortado.
 */
void anexarTexto(char* destino, uint8_t tamanho, const char* texto) {
  uint8_t posicao = (uint8_t)strlen(destino);
  while (*texto != '\0' && (uint8_t)(posicao + 1) < tamanho) {
    destino[posicao++] = *texto++;
  }
  destino[posicao] = '\0';
}

/**
 * Acrescenta um valor com 1 casa decimal, ou "nan" quando a leitura falhou.
 * dtostrf() é a conversão de float para texto da avr-libc (o printf do
 * Arduino não formata float).
 */
void anexarValor(char* destino, uint8_t tamanho, float valor) {
  if (isnan(valor)) {
    anexarTexto(destino, tamanho, "nan");
    return;
  }
  char campo[TAMANHO_CAMPO_NUMERICO];
  dtostrf(valor, 1, 1, campo);
  anexarTexto(destino, tamanho, campo);
}

/**
 * XOR de todos os bytes do conteúdo (o trecho entre '$' e '*').
 */
uint8_t calcularChecksum(const char* conteudo) {
  uint8_t soma = 0;
  while (*conteudo != '\0') {
    soma ^= (uint8_t)(*conteudo++);
  }
  return soma;
}

/**
 * Monta e envia "$2,<t>,<ur>,<solo>*XX\r\n" (protocolo 2 com checksum).
 */
void enviarLinhaProtocolo(float temperaturaAr, float umidadeAr, float umidadeSolo) {
  static const char HEXADECIMAL[] = "0123456789ABCDEF";
  char conteudo[TAMANHO_LINHA_PROTOCOLO];
  conteudo[0] = '\0';
  anexarTexto(conteudo, sizeof(conteudo), VERSAO_PROTOCOLO);
  anexarTexto(conteudo, sizeof(conteudo), ",");
  anexarValor(conteudo, sizeof(conteudo), temperaturaAr);
  anexarTexto(conteudo, sizeof(conteudo), ",");
  anexarValor(conteudo, sizeof(conteudo), umidadeAr);
  anexarTexto(conteudo, sizeof(conteudo), ",");
  anexarValor(conteudo, sizeof(conteudo), umidadeSolo);

  const uint8_t checksum = calcularChecksum(conteudo);
  char sufixo[4];
  sufixo[0] = '*';
  sufixo[1] = HEXADECIMAL[(checksum >> 4) & 0x0F];
  sufixo[2] = HEXADECIMAL[checksum & 0x0F];
  sufixo[3] = '\0';

  Serial.print('$');
  Serial.print(conteudo);
  Serial.println(sufixo);
}

/**
 * Lê o DHT e o higrômetro (média móvel) e envia a linha do protocolo 2.
 * Com alimentação pulsada, a amostra do solo é feita aqui (1 pulso por envio).
 * Se o DHT11 devolver NaN, envia o diagnóstico de erro E a linha de dados
 * com "nan" no ar: a umidade do solo nunca deixa de chegar ao coletor por
 * causa de uma falha do sensor do ar.
 */
void lerSensoresEEnviar() {
  float umidadeAr = dht.readHumidity();
  float temperaturaAr = dht.readTemperature();   // em graus Celsius (negativa com DHT22)

#if AGRO_PINO_ALIMENTACAO_SOLO >= 0
  amostrarHigrometro();
#endif
  const float mediaAdc = mediaAdcSolo();
  verificarHigrometro(mediaAdc);
  const float umidadeSolo = converterAdcParaPercentual(mediaAdc);

  if (isnan(temperaturaAr) || isnan(umidadeAr)) {
    tratarFalhaDht();
    temperaturaAr = NAN;
    umidadeAr = NAN;
  } else {
    falhasDhtSeguidas = 0;
  }

  enviarLinhaProtocolo(temperaturaAr, umidadeAr, umidadeSolo);
  acenderLedHeartbeat(millis());
}

/**
 * Registra uma leitura inválida do DHT11. Após MAXIMO_FALHAS_DHT_SEGUIDAS
 * falhas consecutivas, reinicializa o sensor com dht.begin() (recupera
 * travamentos do barramento de um fio causados por ruído ou mau contato).
 */
void tratarFalhaDht() {
  Serial.println(F("#ERRO:" NOME_SENSOR_AR "_LEITURA_INVALIDA"));
  if (falhasDhtSeguidas < 255) {
    falhasDhtSeguidas++;
  }
  if (falhasDhtSeguidas >= MAXIMO_FALHAS_DHT_SEGUIDAS) {
    dht.begin();
    falhasDhtSeguidas = 0;
    Serial.println(F("#AVISO:" NOME_SENSOR_AR "_REINICIADO"));
  }
}

/**
 * Acende o LED embutido e marca o instante, para apagá-lo depois sem delay().
 */
void acenderLedHeartbeat(uint32_t agora) {
  digitalWrite(LED_BUILTIN, HIGH);
  ledAceso = true;
  instanteLedAceso = agora;
}

/**
 * Apaga o LED embutido quando o pulso do heartbeat já durou o suficiente.
 */
void atualizarLedHeartbeat(uint32_t agora) {
  if (ledAceso && (uint32_t)(agora - instanteLedAceso) >= DURACAO_PULSO_LED_MS) {
    digitalWrite(LED_BUILTIN, LOW);
    ledAceso = false;
  }
}

/**
 * Configuração inicial: Serial a 9600 bps, LED, DHT, pino da sonda pulsada e
 * pré-preenchimento do buffer da média móvel. Ao final anuncia
 * "#AGROANALISE_PRONTO;PROTOCOLO=2", o perfil dos sensores
 * ("#AGROANALISE_PERFIL;...;AR=DHT11;SOLO=RESISTIVO;PULSO=0") e liga o
 * watchdog de 8 s.
 */
void setup() {
#if defined(__AVR__) && AGRO_USAR_WATCHDOG
  // Depois de um reinício pelo watchdog ele continua ligado com o prazo
  // mínimo: limpa o motivo do reinício e desliga já, antes do setup
  // demorado; religa no final.
  MCUSR = 0;
  wdt_disable();
#endif
  Serial.begin(9600);

  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);
  pinMode(PINO_HIGROMETRO, INPUT);
#if AGRO_PINO_ALIMENTACAO_SOLO >= 0
  pinMode(AGRO_PINO_ALIMENTACAO_SOLO, OUTPUT);
  digitalWrite(AGRO_PINO_ALIMENTACAO_SOLO, LOW);   // sonda desligada entre as medidas
#endif

  dht.begin();
  preencherBufferSolo();

  const uint32_t agora = millis();
  instanteUltimaAmostraSolo = agora;
  // A primeira leitura do DHT acontece INTERVALO_ENVIO_MS após o setup, o que
  // também respeita o tempo de estabilização do sensor depois de energizado.
  instanteUltimoEnvio = agora;

  Serial.println(F("#AGROANALISE_PRONTO;PROTOCOLO=2"));
  Serial.println(F("#AGROANALISE_PERFIL;PROTOCOLO=2;AR=" NOME_SENSOR_AR ";SOLO=RESISTIVO;PULSO=" TEXTO_PULSO));

#if defined(__AVR__) && AGRO_USAR_WATCHDOG
  wdt_enable(WDTO_8S);
#endif
}

/**
 * Laço principal NÃO bloqueante: cada tarefa verifica se já chegou a sua vez
 * comparando millis() com o instante da última execução. A subtração em
 * aritmética sem sinal (uint32_t) continua correta quando millis() "dá a
 * volta" (estouro após ~49,7 dias ligado).
 */
void loop() {
#if defined(__AVR__) && AGRO_USAR_WATCHDOG
  wdt_reset();   // "estou vivo": sem isto por 8 s, a placa reinicia sozinha
#endif
  const uint32_t agora = millis();

#if AGRO_PINO_ALIMENTACAO_SOLO < 0
  // Sonda ligada direto no 5V: amostra a cada 100 ms. Com alimentação
  // pulsada, a amostra é feita uma vez por envio, em lerSensoresEEnviar().
  if ((uint32_t)(agora - instanteUltimaAmostraSolo) >= INTERVALO_AMOSTRA_SOLO_MS) {
    instanteUltimaAmostraSolo += INTERVALO_AMOSTRA_SOLO_MS;
    // Se o loop atrasou muito (ex.: leitura lenta do DHT), ressincroniza em vez
    // de disparar uma rajada de amostras atrasadas.
    if ((uint32_t)(agora - instanteUltimaAmostraSolo) >= INTERVALO_AMOSTRA_SOLO_MS) {
      instanteUltimaAmostraSolo = agora;
    }
    amostrarHigrometro();
  }
#endif

  if ((uint32_t)(agora - instanteUltimoEnvio) >= INTERVALO_ENVIO_MS) {
    instanteUltimoEnvio += INTERVALO_ENVIO_MS;
    if ((uint32_t)(agora - instanteUltimoEnvio) >= INTERVALO_ENVIO_MS) {
      instanteUltimoEnvio = agora;
    }
    lerSensoresEEnviar();
  }

  atualizarLedHeartbeat(millis());
}
