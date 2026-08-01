import serial
import struct
import time
import paho.mqtt.client as mqtt

# ─── CONFIGURACIÓN ────────────────────────────────────────────────
SERIAL_PORT = "COM3"        # Cambia al puerto que viste en el Adm. de dispositivos
BAUD_RATE   = 19200         # Vantage Pro 2 usa 19200 por defecto
MQTT_BROKER = "localhost"   # IP de tu broker MQTT (ej: Mosquitto)
MQTT_PORT   = 1883
MQTT_PREFIX = "weather"     # Los topics serán: weather/temperatura, etc.
INTERVALO   = 60            # Segundos entre lecturas
# ──────────────────────────────────────────────────────────────────

def conectar_serial(port, baud):
    ser = serial.Serial(
        port=port,
        baudrate=baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=5
    )
    return ser

def despertar_estacion(ser):
    """El Vantage Pro 2 necesita un 'wakeup' antes de responder."""
    for _ in range(3):
        ser.write(b'\n')
        time.sleep(1.2)
        resp = ser.read(ser.in_waiting or 2)
        if b'\n\r' in resp or b'\r\n' in resp:
            print("✅ Estación despierta")
            return True
    print("⚠️  No se pudo despertar la estación")
    return False

def calcular_crc(data: bytes) -> int:
    """CRC-16 según protocolo Davis."""
    CRC_TABLE = [
        0x0000,0x1021,0x2042,0x3063,0x4084,0x50a5,0x60c6,0x70e7,
        0x8108,0x9129,0xa14a,0xb16b,0xc18c,0xd1ad,0xe1ce,0xf1ef,
    ]
    crc = 0
    for byte in data:
        crc = ((crc << 4) & 0xFFFF) ^ CRC_TABLE[((crc >> 12) ^ (byte >> 4)) & 0x0F]
        crc = ((crc << 4) & 0xFFFF) ^ CRC_TABLE[((crc >> 12) ^ (byte & 0x0F)) & 0x0F]
    return crc

def leer_loop(ser) -> dict | None:
    """
    Envía comando LOOP 1 y parsea la respuesta de 99 bytes.
    Retorna un dict con los valores o None si hay error.
    """
    ser.reset_input_buffer()
    ser.write(b'LOOP 1\n')
    time.sleep(1)

    # ACK = 0x06 seguido de 99 bytes de datos
    ack = ser.read(1)
    if ack != b'\x06':
        print(f"⚠️  ACK inesperado: {ack.hex()}")
        return None

    data = ser.read(99)
    if len(data) < 99:
        print(f"⚠️  Datos incompletos: {len(data)} bytes")
        return None

    # Verificar CRC (últimos 2 bytes)
    if calcular_crc(data) != 0:
        print("⚠️  Error de CRC")
        return None

    # ── Parsear campos del paquete LOOP ──────────────────────────
    # Referencia: Davis Serial Communication Reference Manual
    bar_trend     = struct.unpack('b', data[3:4])[0]       # tendencia barométrica
    presion_raw   = struct.unpack('<H', data[7:9])[0]       # en 1/1000 inHg
    temp_interior = struct.unpack('<h', data[9:11])[0]      # en 1/10 °F
    hum_interior  = data[11]                                 # %
    temp_exterior = struct.unpack('<h', data[12:14])[0]     # en 1/10 °F
    vel_viento    = data[14]                                 # mph
    vel_viento_10 = struct.unpack('<H', data[15:17])[0]     # avg 10min mph
    dir_viento    = struct.unpack('<H', data[17:19])[0]     # grados 0-359
    hum_exterior  = data[33]                                 # %

    # ── Conversiones ─────────────────────────────────────────────
    presion_hpa    = round(presion_raw * 0.033864, 2)       # inHg → hPa
    temp_ext_c     = round((temp_exterior / 10 - 32) * 5/9, 1)  # °F → °C
    temp_int_c     = round((temp_interior / 10 - 32) * 5/9, 1)
    vel_kmh        = round(vel_viento * 1.60934, 1)         # mph → km/h
    vel_kmh_10     = round(vel_viento_10 * 1.60934, 1)

    return {
        "temperatura_exterior": temp_ext_c,
        "temperatura_interior": temp_int_c,
        "humedad_exterior":     hum_exterior,
        "humedad_interior":     hum_interior,
        "presion":              presion_hpa,
        "velocidad_viento":     vel_kmh,
        "velocidad_viento_10m": vel_kmh_10,
        "direccion_viento":     dir_viento,
    }

def publicar_mqtt(client, datos: dict):
    for clave, valor in datos.items():
        topic = f"{MQTT_PREFIX}/{clave}"
        client.publish(topic, str(valor), retain=True)
        print(f"  📤 {topic} → {valor}")

# ─── MAIN ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Conectar MQTT
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT)
    mqtt_client.loop_start()

    # Conectar serial
    print(f"🔌 Conectando a {SERIAL_PORT}...")
    ser = conectar_serial(SERIAL_PORT, BAUD_RATE)
    
    if not despertar_estacion(ser):
        exit(1)

    print(f"🌤️  Iniciando lecturas cada {INTERVALO}s...\n")

    try:
        while True:
            datos = leer_loop(ser)
            if datos:
                print(f"📊 {time.strftime('%H:%M:%S')} - Datos leídos:")
                for k, v in datos.items():
                    print(f"   {k}: {v}")
                publicar_mqtt(mqtt_client, datos)
                print()
            time.sleep(INTERVALO)

    except KeyboardInterrupt:
        print("\n🛑 Detenido por el usuario")
    finally:
        ser.close()
        mqtt_client.loop_stop()
        mqtt_client.disconnect()