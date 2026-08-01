import serial
import serial.tools.list_ports
import struct
import time
import os

# ══════════════════════════════════════════════
#  CONFIGURACIÓN  — ajusta solo esto
# ══════════════════════════════════════════════
SERIAL_PORT = "COM3"   # <-- cambia según tu Adm. de dispositivos
BAUD_RATE   = 19200
INTERVALO   = 10       # segundos entre lecturas
# ══════════════════════════════════════════════


def listar_puertos():
    """Muestra todos los puertos COM disponibles para ayudar a identificar el correcto."""
    puertos = serial.tools.list_ports.comports()
    if not puertos:
        print("  [!] No se encontraron puertos COM disponibles.")
        return
    print("\n  Puertos COM detectados:")
    for p in puertos:
        print(f"    {p.device:10} → {p.description}")
    print()


def calcular_crc(data: bytes) -> int:
    """CRC-16 CCITT según protocolo Davis."""
    crc = 0
    for byte in data:
        crc = ((crc << 4) & 0xFFFF) ^ [
            0x0000,0x1021,0x2042,0x3063,0x4084,0x50a5,0x60c6,0x70e7,
            0x8108,0x9129,0xa14a,0xb16b,0xc18c,0xd1ad,0xe1ce,0xf1ef
        ][((crc >> 12) ^ (byte >> 4)) & 0x0F]
        crc = ((crc << 4) & 0xFFFF) ^ [
            0x0000,0x1021,0x2042,0x3063,0x4084,0x50a5,0x60c6,0x70e7,
            0x8108,0x9129,0xa14a,0xb16b,0xc18c,0xd1ad,0xe1ce,0xf1ef
        ][((crc >> 12) ^ (byte & 0x0F)) & 0x0F]
    return crc


def despertar_estacion(ser: serial.Serial) -> bool:
    """
    El Vantage Pro 2 entra en modo sleep. Hay que enviarle
    saltos de línea hasta recibir \\n\\r como confirmación.
    """
    print("  Despertando estación", end="", flush=True)
    for intento in range(3):
        ser.reset_input_buffer()
        ser.write(b'\n')
        time.sleep(1.2)
        respuesta = ser.read(ser.in_waiting or 2)
        if b'\n\r' in respuesta or b'\r\n' in respuesta:
            print(" ✓")
            return True
        print(".", end="", flush=True)
    print(" ✗")
    return False


def probar_conexion(ser: serial.Serial) -> bool:
    """
    Envía el comando TEST. La estación debe responder 'TEST\\n\\r'.
    Es la forma más rápida de verificar que la comunicación funciona.
    """
    ser.reset_input_buffer()
    ser.write(b'TEST\n')
    time.sleep(0.5)
    respuesta = ser.read(ser.in_waiting or 10)
    if b'TEST' in respuesta:
        print(f"  Comando TEST respondido correctamente → {respuesta!r}")
        return True
    else:
        print(f"  [!] Respuesta inesperada al TEST → {respuesta!r}")
        return False


def leer_version(ser: serial.Serial):
    """Lee la versión del firmware (comando VER)."""
    ser.reset_input_buffer()
    ser.write(b'VER\n')
    time.sleep(0.5)
    resp = ser.read(ser.in_waiting or 20).decode(errors='replace').strip()
    print(f"  Firmware: {resp}")


def grados_a_rosa(grados: int) -> str:
    """Convierte grados a rosa de los vientos (N, NE, E, etc.)."""
    rosas = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
             "S","SSO","SO","OSO","O","ONO","NO","NNO"]
    idx = round(grados / 22.5) % 16
    return rosas[idx]


def leer_loop(ser: serial.Serial) -> dict | None:
    """
    Envía LOOP 1 y parsea el paquete de 99 bytes.
    Retorna diccionario con todos los sensores o None si hay error.
    """
    ser.reset_input_buffer()
    ser.write(b'LOOP 1\n')
    time.sleep(1)

    # Esperar ACK (0x06)
    ack = ser.read(1)
    if ack != b'\x06':
        print(f"  [!] ACK incorrecto: {ack.hex() if ack else 'sin respuesta'}")
        return None

    data = ser.read(99)
    if len(data) < 99:
        print(f"  [!] Paquete incompleto: {len(data)}/99 bytes recibidos")
        return None

    # Verificar CRC (si el resultado es 0, el paquete es válido)
    if calcular_crc(data) != 0:
        print("  [!] Error de CRC — datos corruptos")
        return None

    # ── Decodificar campos según el manual Davis ──────────────────
    bar_trend     = struct.unpack('b',  data[3:4])[0]
    presion_raw   = struct.unpack('<H', data[7:9])[0]
    temp_int_raw  = struct.unpack('<h', data[9:11])[0]
    hum_interior  = data[11]
    temp_ext_raw  = struct.unpack('<h', data[12:14])[0]
    vel_viento    = data[14]                               # mph
    vel_media_10m = struct.unpack('<H', data[15:17])[0]   # mph (media 10 min)
    dir_viento    = struct.unpack('<H', data[17:19])[0]   # 0-359 grados
    hum_exterior  = data[33]

    # ── Conversiones ─────────────────────────────────────────────
    temp_ext_c    = round((temp_ext_raw / 10 - 32) * 5 / 9, 1)
    temp_int_c    = round((temp_int_raw / 10 - 32) * 5 / 9, 1)
    presion_hpa   = round(presion_raw * 0.033864, 1)
    vel_kmh       = round(vel_viento * 1.60934, 1)
    vel_media_kmh = round(vel_media_10m * 1.60934, 1)

    tendencias = {-60: "Bajando rápido", -20: "Bajando", 0: "Estable",
                   20: "Subiendo",        60: "Subiendo rápido"}
    tendencia = tendencias.get(bar_trend, f"({bar_trend})")

    return {
        "temp_exterior":   temp_ext_c,
        "temp_interior":   temp_int_c,
        "hum_exterior":    hum_exterior,
        "hum_interior":    hum_interior,
        "presion_hpa":     presion_hpa,
        "presion_tendencia": tendencia,
        "vel_viento_kmh":  vel_kmh,
        "vel_media_kmh":   vel_media_kmh,
        "dir_viento_deg":  dir_viento,
        "dir_viento_rosa": grados_a_rosa(dir_viento),
    }


def mostrar_datos(datos: dict, lectura: int):
    """Imprime los datos con formato legible en terminal."""
    os.system('cls' if os.name == 'nt' else 'clear')
    ts = time.strftime('%Y-%m-%d  %H:%M:%S')

    print("╔══════════════════════════════════════════════╗")
    print(f"║   Davis Vantage Pro 2  —  Lectura #{lectura:<5}      ║")
    print(f"║   {ts}                         ║")
    print("╠══════════════════════════════════════════════╣")
    print("║  TEMPERATURA                                 ║")
    print(f"║    Exterior   : {datos['temp_exterior']:>6.1f} °C                    ║")
    print(f"║    Interior   : {datos['temp_interior']:>6.1f} °C                    ║")
    print("╠══════════════════════════════════════════════╣")
    print("║  HUMEDAD                                     ║")
    print(f"║    Exterior   : {datos['hum_exterior']:>6} %                      ║")
    print(f"║    Interior   : {datos['hum_interior']:>6} %                      ║")
    print("╠══════════════════════════════════════════════╣")
    print("║  PRESIÓN BAROMÉTRICA                         ║")
    print(f"║    Presión    : {datos['presion_hpa']:>7.1f} hPa                  ║")
    print(f"║    Tendencia  : {datos['presion_tendencia']:<29} ║")
    print("╠══════════════════════════════════════════════╣")
    print("║  VIENTO                                      ║")
    print(f"║    Velocidad  : {datos['vel_viento_kmh']:>6.1f} km/h                  ║")
    print(f"║    Media 10m  : {datos['vel_media_kmh']:>6.1f} km/h                  ║")
    print(f"║    Dirección  : {datos['dir_viento_deg']:>5}° ({datos['dir_viento_rosa']:<3})               ║")
    print("╚══════════════════════════════════════════════╝")
    print(f"\n  Próxima lectura en {INTERVALO}s...  [Ctrl+C para salir]\n")


# ══════════════════════════════════════════════
#  PROGRAMA PRINCIPAL
# ══════════════════════════════════════════════
if __name__ == "__main__":
    print("\n" + "="*50)
    print("  Davis Vantage Pro 2  —  Test de comunicación")
    print("="*50)

    # 1. Mostrar puertos disponibles
    listar_puertos()

    # 2. Abrir puerto serial
    print(f"  Conectando a {SERIAL_PORT} @ {BAUD_RATE} baud...")
    try:
        ser = serial.Serial(
            port=SERIAL_PORT, baudrate=BAUD_RATE,
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE, timeout=5
        )
        print("  Puerto abierto correctamente ✓")
    except serial.SerialException as e:
        print(f"\n  [ERROR] No se pudo abrir {SERIAL_PORT}: {e}")
        print("  Verifica el puerto en el Administrador de dispositivos.")
        exit(1)

    # 3. Despertar y verificar
    if not despertar_estacion(ser):
        print("\n  [ERROR] La estación no responde al wakeup.")
        print("  Comprueba el cable, el puerto y que la consola esté encendida.")
        ser.close()
        exit(1)

    probar_conexion(ser)
    leer_version(ser)

    print("\n  Comunicación OK — iniciando lecturas...\n")
    time.sleep(1)

    # 4. Bucle de lectura
    lectura = 1
    try:
        while True:
            datos = leer_loop(ser)
            if datos:
                mostrar_datos(datos, lectura)
                lectura += 1
            else:
                print("  [!] Reintentando wakeup...")
                despertar_estacion(ser)
            time.sleep(INTERVALO)

    except KeyboardInterrupt:
        print("\n  Lectura detenida por el usuario.")
    finally:
        ser.close()
        print("  Puerto serial cerrado. ¡Hasta luego!\n")