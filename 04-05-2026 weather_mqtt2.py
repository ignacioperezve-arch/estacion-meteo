import serial
import serial.tools.list_ports
import struct
import time
import math
import os
import paho.mqtt.client as mqtt

# ══════════════════════════════════════════════════════════════════
#  CONFIGURACIÓN SERIAL  — ajusta el puerto COM según tu equipo
# ══════════════════════════════════════════════════════════════════
SERIAL_PORT = "COM3"
BAUD_RATE   = 19200

# ══════════════════════════════════════════════════════════════════
#  CONFIGURACIÓN MQTT
# ══════════════════════════════════════════════════════════════════
MQTT_HOST     = "161.35.98.146"
MQTT_PORT     = 1883
MQTT_USER     = "ignacio"
MQTT_PASSWORD = "Uv2025doc."
MQTT_QOS      = 1
MQTT_RETAIN   = True

# Topics de sensores
TOPIC_TEMP_EXT   = "weather/temperatura_exterior"
TOPIC_HUM_EXT    = "weather/humedad_exterior"
TOPIC_VEL_VIENTO = "weather/velocidad_viento"
TOPIC_DIR_VIENTO = "weather/direccion_viento"
TOPIC_PRESION    = "weather/presion"

# Topics de estado y tiempo
TOPIC_STATUS     = "weather/status"
TOPIC_FECHA      = "weather/fecha"
TOPIC_HORA       = "weather/hora"

# ══════════════════════════════════════════════════════════════════
#  MINUTOS en los que se dispara la captura
# ══════════════════════════════════════════════════════════════════
MINUTOS_CAPTURA = {0, 15, 30, 45}


# ──────────────────────────────────────────────────────────────────
#  UTILIDADES
# ──────────────────────────────────────────────────────────────────

def redondear_temp(valor: float) -> int:
    """
    Decimal >= 0.5 -> entero superior   |   Decimal < 0.5 -> entero inferior
    Ejemplos: 18.5->19 | 18.4->18 | -3.5->-3 | -3.6->-4
    """
    parte_entera = math.floor(valor)
    decimal      = valor - parte_entera
    return parte_entera + 1 if decimal >= 0.5 else parte_entera


def grados_a_rosa(grados: int) -> str:
    rosas = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
             "S","SSO","SO","OSO","O","ONO","NO","NNO"]
    return rosas[round(grados / 22.5) % 16]


def segundos_hasta_proximo_cuarto() -> float:
    """Segundos que faltan para el proximo :00, :15, :30 o :45."""
    ahora            = time.localtime()
    segundos_en_hora = ahora.tm_min * 60 + ahora.tm_sec
    proximos         = [m * 60 for m in sorted(MINUTOS_CAPTURA)]
    for p in proximos:
        diff = p - segundos_en_hora
        if diff > 5:
            return diff
    return 3600 - segundos_en_hora + proximos[0]


def listar_puertos():
    puertos = serial.tools.list_ports.comports()
    if not puertos:
        print("  [!] No se encontraron puertos COM disponibles.")
        return
    print("\n  Puertos COM detectados:")
    for p in puertos:
        print(f"    {p.device:10} -> {p.description}")
    print()


# ──────────────────────────────────────────────────────────────────
#  CRC-16 CCITT (protocolo Davis)
# ──────────────────────────────────────────────────────────────────

def calcular_crc(data: bytes) -> int:
    tabla = [
        0x0000,0x1021,0x2042,0x3063,0x4084,0x50a5,0x60c6,0x70e7,
        0x8108,0x9129,0xa14a,0xb16b,0xc18c,0xd1ad,0xe1ce,0xf1ef
    ]
    crc = 0
    for byte in data:
        crc = ((crc << 4) & 0xFFFF) ^ tabla[((crc >> 12) ^ (byte >> 4)) & 0x0F]
        crc = ((crc << 4) & 0xFFFF) ^ tabla[((crc >> 12) ^ (byte & 0x0F)) & 0x0F]
    return crc


# ──────────────────────────────────────────────────────────────────
#  SERIAL — COMUNICACIÓN CON DAVIS
# ──────────────────────────────────────────────────────────────────

def despertar_estacion(ser: serial.Serial) -> bool:
    print("  Despertando estacion", end="", flush=True)
    for _ in range(3):
        ser.reset_input_buffer()
        ser.write(b'\n')
        time.sleep(1.2)
        r = ser.read(ser.in_waiting or 2)
        if b'\n\r' in r or b'\r\n' in r:
            print(" OK")
            return True
        print(".", end="", flush=True)
    print(" FALLO")
    return False


def probar_conexion(ser: serial.Serial) -> bool:
    ser.reset_input_buffer()
    ser.write(b'TEST\n')
    time.sleep(0.5)
    r = ser.read(ser.in_waiting or 10)
    ok = b'TEST' in r
    print(f"  TEST -> {'OK' if ok else 'FALLO  ' + repr(r)}")
    return ok


def leer_version(ser: serial.Serial):
    ser.reset_input_buffer()
    ser.write(b'VER\n')
    time.sleep(0.5)
    resp = ser.read(ser.in_waiting or 20).decode(errors='replace').strip()
    print(f"  Firmware: {resp}")


def leer_loop(ser: serial.Serial):
    ser.reset_input_buffer()
    ser.write(b'LOOP 1\n')
    time.sleep(1)

    ack = ser.read(1)
    if ack != b'\x06':
        print(f"  [!] ACK incorrecto: {ack.hex() if ack else 'sin respuesta'}")
        return None

    data = ser.read(99)
    if len(data) < 99:
        print(f"  [!] Paquete incompleto: {len(data)}/99 bytes")
        return None

    if calcular_crc(data) != 0:
        print("  [!] Error de CRC")
        return None

    # Parseo binario segun manual Davis
    bar_trend     = struct.unpack('b',  data[3:4])[0]
    presion_raw   = struct.unpack('<H', data[7:9])[0]
    temp_int_raw  = struct.unpack('<h', data[9:11])[0]
    hum_interior  = data[11]
    temp_ext_raw  = struct.unpack('<h', data[12:14])[0]
    vel_viento    = data[14]
    vel_media_10m = struct.unpack('<H', data[15:17])[0]
    dir_viento    = struct.unpack('<H', data[17:19])[0]
    hum_exterior  = data[33]

    # Conversiones
    temp_ext_c_raw = (temp_ext_raw / 10 - 32) * 5 / 9
    temp_int_c_raw = (temp_int_raw / 10 - 32) * 5 / 9
    presion_hpa    = round(presion_raw * 0.033864, 1)

    # Velocidad: mph -> m/s  (1 mph = 0.44704 m/s)
    #vel_ms        = round(vel_viento * 0.44704, 1)
    #vel_media_ms  = round(vel_media_10m * 0.44704, 1)
    vel_ms        = round(vel_viento * (1/3.6), 1)
    vel_media_ms  = round(vel_media_10m * (1/3.6), 1)

    tendencias = {
        -60: "Bajando rapido",
        -20: "Bajando",
          0: "Estable",
         20: "Subiendo",
         60: "Subiendo rapido"
    }
    tendencia = tendencias.get(bar_trend, f"({bar_trend})")

    return {
        "temp_exterior_raw": round(temp_ext_c_raw, 1),  # con decimal (para terminal)
        "temp_interior_raw": round(temp_int_c_raw, 1),
        "temp_exterior":     redondear_temp(temp_ext_c_raw),  # entero (para MQTT)
        "temp_interior":     redondear_temp(temp_int_c_raw),
        "hum_exterior":      hum_exterior,
        "hum_interior":      hum_interior,
        "presion_hpa":       presion_hpa,
        "presion_tendencia": tendencia,
        "vel_viento_ms":     vel_ms,
        "vel_media_ms":      vel_media_ms,
        "dir_viento_deg":    dir_viento,
        "dir_viento_rosa":   grados_a_rosa(dir_viento),
    }


# ──────────────────────────────────────────────────────────────────
#  MQTT  — compatible con paho-mqtt 2.x
# ──────────────────────────────────────────────────────────────────

mqtt_conectado = False   # variable global de estado


def crear_cliente_mqtt() -> mqtt.Client:
    global mqtt_conectado

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="davis_vantage_pro2"
    )
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

    def on_connect(c, userdata, flags, reason_code, properties):
        global mqtt_conectado
        if reason_code.value == 0:
            mqtt_conectado = True
            print("  MQTT -> Conectado OK")
        else:
            mqtt_conectado = False
            print(f"  MQTT -> Error: {reason_code.getName()}  (valor={reason_code.value})")

    def on_disconnect(c, userdata, flags, reason_code, properties):
        global mqtt_conectado
        mqtt_conectado = False
        try:
            val    = reason_code.value
            nombre = reason_code.getName()
        except AttributeError:
            val    = reason_code
            nombre = str(reason_code)
        if val != 0:
            print(f"  [!] MQTT desconectado: {nombre} (valor={val})")

    def on_publish(c, userdata, mid, reason_code, properties):
        pass

    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_publish    = on_publish

    return client


def publicar(client: mqtt.Client, datos: dict, ts: str) -> bool:
    """
    Publica los 5 topics de sensores mas los 3 topics de estado:
      weather/status  -> "OK"
      weather/fecha   -> "2026-04-29"
      weather/hora    -> "14:58:00"
    """
    fecha = ts[:10]    # "2026-04-29"
    hora  = ts[11:]    # "14:58:00"

    payloads = {
        TOPIC_TEMP_EXT:   str(datos["temp_exterior"]),
        TOPIC_HUM_EXT:    str(datos["hum_exterior"]),
        TOPIC_VEL_VIENTO: str(datos["vel_viento_ms"]),
        TOPIC_DIR_VIENTO: str(datos["dir_viento_deg"]),
        TOPIC_PRESION:    str(datos["presion_hpa"]),
        TOPIC_STATUS:     "OK",
        TOPIC_FECHA:      fecha,
        TOPIC_HORA:       hora,
    }
    todo_ok = True
    for topic, payload in payloads.items():
        result = client.publish(topic, payload, qos=MQTT_QOS, retain=MQTT_RETAIN)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            print(f"  [!] Error publicando {topic} (rc={result.rc})")
            todo_ok = False
    return todo_ok


# ──────────────────────────────────────────────────────────────────
#  PANEL TERMINAL
# ──────────────────────────────────────────────────────────────────

def mostrar_panel(datos: dict, lectura: int, mqtt_ok: bool, proxima: str):
    os.system('cls' if os.name == 'nt' else 'clear')
    ts     = time.strftime('%Y-%m-%d  %H:%M:%S')
    estado = "OK - Publicado" if mqtt_ok else "ERROR"

    print("+=================================================+")
    print(f"|  Davis VP2 + MQTT          Lectura #{lectura:<5}       |")
    print(f"|  {ts}                          |")
    print("+====================+============================+")
    print("|  TEMPERATURA       |                            |")
    print(f"|  Exterior (raw)    |  {datos['temp_exterior_raw']:>6.1f} grados C           |")
    print(f"|  Exterior (MQTT)   |  {datos['temp_exterior']:>6} grados C  redondeado  |")
    print(f"|  Interior          |  {datos['temp_interior_raw']:>6.1f} grados C           |")
    print("+====================+============================+")
    print("|  HUMEDAD           |                            |")
    print(f"|  Exterior          |  {datos['hum_exterior']:>6} %                    |")
    print(f"|  Interior          |  {datos['hum_interior']:>6} %                    |")
    print("+====================+============================+")
    print("|  PRESION           |                            |")
    print(f"|  Presion           |  {datos['presion_hpa']:>7.1f} hPa                |")
    print(f"|  Tendencia         |  {datos['presion_tendencia']:<26}  |")
    print("+====================+============================+")
    print("|  VIENTO            |                            |")
    print(f"|  Velocidad         |  {datos['vel_viento_ms']:>6.1f} m/s                |")
    print(f"|  Media 10 min      |  {datos['vel_media_ms']:>6.1f} m/s                |")
    print(f"|  Direccion         |  {datos['dir_viento_deg']:>5} grados ({datos['dir_viento_rosa']:<3})          |")
    print("+====================+============================+")
    print(f"|  MQTT estado       |  {estado:<26}  |")
    print(f"|  Proxima captura   |  {proxima:<26}  |")
    print("+=================================================+")
    print("  [Ctrl+C para salir]")


# ══════════════════════════════════════════════════════════════════
#  PROGRAMA PRINCIPAL
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":

    print("\n" + "=" * 52)
    print("  Davis Vantage Pro 2  --  MQTT cada cuarto de hora")
    print("=" * 52)

    # ── 1. Puerto serial ─────────────────────────────────────────
    listar_puertos()
    print(f"  Conectando a {SERIAL_PORT} @ {BAUD_RATE} baud...")
    try:
        ser = serial.Serial(
            port     = SERIAL_PORT,
            baudrate = BAUD_RATE,
            bytesize = serial.EIGHTBITS,
            parity   = serial.PARITY_NONE,
            stopbits = serial.STOPBITS_ONE,
            timeout  = 5
        )
        print("  Puerto serial abierto OK")
    except serial.SerialException as e:
        print(f"\n  [ERROR] No se pudo abrir {SERIAL_PORT}: {e}")
        exit(1)

    if not despertar_estacion(ser):
        print("\n  [ERROR] La estacion no responde al wakeup.")
        ser.close()
        exit(1)

    probar_conexion(ser)
    leer_version(ser)

    # ── 2. MQTT ──────────────────────────────────────────────────
    print(f"\n  Conectando MQTT -> {MQTT_HOST}:{MQTT_PORT} ...")
    mqtt_client = crear_cliente_mqtt()
    try:
        mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        mqtt_client.loop_start()
        time.sleep(2)
    except Exception as e:
        print(f"  [ERROR MQTT] {e}")
        ser.close()
        exit(1)

    if not mqtt_conectado:
        print("  [!] No se pudo conectar al broker. Verifica host, puerto y credenciales.")
        ser.close()
        mqtt_client.loop_stop()
        exit(1)

    print("\n  Sistema listo. Esperando proximo cuarto de hora...\n")

    lectura = 1
    mqtt_ok = False
    datos   = {}

    try:
        while True:
            espera      = segundos_hasta_proximo_cuarto()
            proxima_str = time.strftime('%H:%M:%S',
                                        time.localtime(time.time() + espera))

            if datos:
                mostrar_panel(datos, lectura - 1, mqtt_ok, proxima_str)
            else:
                print(f"  Esperando... primera captura a las {proxima_str} "
                      f"({espera:.0f} s)   [Ctrl+C para salir]", end="\r")

            # Espera en bloques de 10 s para refrescar la cuenta regresiva
            tiempo_restante = espera
            while tiempo_restante > 0:
                pausa = min(10, tiempo_restante)
                time.sleep(pausa)
                tiempo_restante -= pausa
                proxima_str = time.strftime(
                    '%H:%M:%S',
                    time.localtime(time.time() + max(0, tiempo_restante))
                )
                if datos:
                    mostrar_panel(datos, lectura - 1, mqtt_ok, proxima_str)

            # ── Momento de captura ────────────────────────────────
            ts_captura = time.strftime('%Y-%m-%d %H:%M:%S')
            print(f"\n  Capturando  {ts_captura}...")

            if not despertar_estacion(ser):
                print("  [!] Wakeup fallido. Reintentando en 30 s...")
                time.sleep(30)
                continue

            datos_nuevos = leer_loop(ser)
            if datos_nuevos is None:
                print("  [!] Error de lectura. Reintentando en 60 s...")
                time.sleep(60)
                continue

            datos = datos_nuevos

            # Reconectar MQTT si se perdio la conexion
            if not mqtt_conectado:
                print("  [!] MQTT desconectado, reconectando...")
                try:
                    mqtt_client.reconnect()
                    time.sleep(2)
                except Exception as e:
                    print(f"  [!] Fallo reconexion MQTT: {e}")

            mqtt_ok = publicar(mqtt_client, datos, ts_captura)

            if mqtt_ok:
                print(f"  Publicado en MQTT:")
                print(f"    {TOPIC_TEMP_EXT:<36} -> {datos['temp_exterior']} C")
                print(f"    {TOPIC_HUM_EXT:<36} -> {datos['hum_exterior']} %")
                print(f"    {TOPIC_VEL_VIENTO:<36} -> {datos['vel_viento_ms']} m/s")
                print(f"    {TOPIC_DIR_VIENTO:<36} -> {datos['dir_viento_deg']} grados")
                print(f"    {TOPIC_PRESION:<36} -> {datos['presion_hpa']} hPa")
                print(f"    {TOPIC_STATUS:<36} -> OK")
                print(f"    {TOPIC_FECHA:<36} -> {ts_captura[:10]}")
                print(f"    {TOPIC_HORA:<36} -> {ts_captura[11:]}")
            else:
                print("  [!] Algunos topics no se publicaron correctamente.")

            lectura += 1
            time.sleep(2)

    except KeyboardInterrupt:
        print("\n\n  Detenido por el usuario.")
    finally:
        mqtt_client.publish(TOPIC_STATUS, "OFFLINE", retain=True)
        mqtt_client.publish(TOPIC_FECHA,  "--",      retain=True)
        mqtt_client.publish(TOPIC_HORA,   "--",      retain=True)
        time.sleep(0.8)
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        ser.close()
        print("  Conexiones cerradas. Hasta luego.\n")
