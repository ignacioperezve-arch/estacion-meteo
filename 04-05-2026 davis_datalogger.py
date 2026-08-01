import serial
import serial.tools.list_ports
import struct
import time
import csv
import os
from datetime import datetime, timedelta

# ══════════════════════════════════════════════════════════════════
#  CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════
SERIAL_PORT  = "COM3"
BAUD_RATE    = 19200
ARCHIVO_CSV  = "datalogger_davis.csv"   # archivo de salida

# ══════════════════════════════════════════════════════════════════
#  CONSTANTES DEL PROTOCOLO DAVIS
# ══════════════════════════════════════════════════════════════════
ACK  = b'\x06'
NAK  = b'\x21'
ESC  = b'\x1b'
CAN  = b'\x18'

REGISTROS_POR_PAGINA = 5     # cada página DMP contiene 5 registros
BYTES_POR_PAGINA     = 267   # 1 byte secuencia + 260 datos + 4 CRC + 2 extra
BYTES_POR_REGISTRO   = 52    # cada registro de archivo ocupa 52 bytes


# ──────────────────────────────────────────────────────────────────
#  CRC-16 CCITT
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
#  COMUNICACIÓN SERIAL
# ──────────────────────────────────────────────────────────────────

def abrir_puerto() -> serial.Serial:
    puertos = serial.tools.list_ports.comports()
    print("  Puertos COM disponibles:")
    for p in puertos:
        print(f"    {p.device:10} -> {p.description}")
    print()

    ser = serial.Serial(
        port     = SERIAL_PORT,
        baudrate = BAUD_RATE,
        bytesize = serial.EIGHTBITS,
        parity   = serial.PARITY_NONE,
        stopbits = serial.STOPBITS_ONE,
        timeout  = 5
    )
    return ser


def despertar(ser: serial.Serial) -> bool:
    print("  Despertando consola", end="", flush=True)
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


def enviar_comando(ser: serial.Serial, cmd: bytes, espera=0.5) -> bytes:
    ser.reset_input_buffer()
    ser.write(cmd)
    time.sleep(espera)
    return ser.read(ser.in_waiting or 64)


# ──────────────────────────────────────────────────────────────────
#  LEER INFORMACIÓN DE LA CONSOLA
# ──────────────────────────────────────────────────────────────────

def leer_info_consola(ser: serial.Serial) -> dict:
    """Lee EEPROM para saber el intervalo de archivo configurado."""
    info = {}

    # Versión firmware
    resp = enviar_comando(ser, b'VER\n')
    info['firmware'] = resp.decode(errors='replace').strip()

    # Intervalo de archivado (dirección EEPROM 0x2D, 1 byte, en minutos)
    ser.reset_input_buffer()
    ser.write(b'EERD 2D 01\n')
    time.sleep(0.5)
    resp = ser.read(ser.in_waiting or 10)
    try:
        # Respuesta: ACK + byte de datos
        if len(resp) >= 2:
            info['intervalo_min'] = resp[-1]
        else:
            info['intervalo_min'] = 5   # valor más común por defecto
    except Exception:
        info['intervalo_min'] = 5

    return info


def leer_cantidad_registros(ser: serial.Serial) -> int:
    """
    Lee el puntero de la memoria de archivo para saber cuántos
    registros hay disponibles. Usa el comando DMPAFT con fecha
    muy antigua para contar páginas totales en la respuesta.
    """
    # El comando GETTIME nos da la hora actual de la consola
    ser.reset_input_buffer()
    ser.write(b'GETTIME\n')
    time.sleep(0.5)
    resp = ser.read(ser.in_waiting or 10)
    return resp   # se usa solo para mostrar info


# ──────────────────────────────────────────────────────────────────
#  PARSEAR UN REGISTRO DE ARCHIVO (Rev B, 52 bytes)
# ──────────────────────────────────────────────────────────────────

def decodificar_fecha_hora(date_stamp: int, time_stamp: int) -> str:
    """
    dateStamp: bits 15-9 = año-2000, bits 8-5 = mes, bits 4-0 = dia
    timeStamp: hora * 100 + minuto
    """
    try:
        dia  = date_stamp & 0x1F
        mes  = (date_stamp >> 5) & 0x0F
        anio = ((date_stamp >> 9) & 0x7F) + 2000
        hora = time_stamp // 100
        min_ = time_stamp % 100
        return f"{anio:04d}-{mes:02d}-{dia:02d} {hora:02d}:{min_:02d}"
    except Exception:
        return "Fecha inválida"


def parsear_registro(data: bytes) -> dict | None:
    """
    Parsea un registro Rev B de 52 bytes segun el manual Davis.
    Retorna None si el registro está vacío (0xFF).
    """
    if len(data) < 52:
        return None

    # Registro vacío
    if data[0] == 0xFF and data[1] == 0xFF:
        return None

    try:
        date_stamp    = struct.unpack('<H', data[0:2])[0]
        time_stamp    = struct.unpack('<H', data[2:4])[0]
        fecha_hora    = decodificar_fecha_hora(date_stamp, time_stamp)

        # Temperatura exterior (1/10 F)
        temp_ext_raw  = struct.unpack('<h', data[4:6])[0]
        temp_ext_c    = round((temp_ext_raw / 10 - 32) * 5 / 9, 1) if temp_ext_raw != 32767 else None

        # Temperatura alta del período (1/10 F)
        temp_hi_raw   = struct.unpack('<H', data[6:8])[0]
        temp_hi_c     = round((temp_hi_raw / 10 - 32) * 5 / 9, 1) if temp_hi_raw != 32767 else None

        # Temperatura baja del período (1/10 F)
        temp_lo_raw   = struct.unpack('<H', data[8:10])[0]
        temp_lo_c     = round((temp_lo_raw / 10 - 32) * 5 / 9, 1) if temp_lo_raw != 32767 else None

        # Lluvia en el período (clics, 1 clic = 0.2mm aprox)
        lluvia_clics  = struct.unpack('<H', data[10:12])[0]
        lluvia_mm     = round(lluvia_clics * 0.2, 1)

        # Velocidad viento máxima (mph)
        vel_hi_raw    = data[12]
        vel_hi_ms     = round(vel_hi_raw * 0.44704, 1) if vel_hi_raw != 255 else None

        # Velocidad viento media (mph)
        vel_avg_raw   = data[13]
        vel_avg_ms    = round(vel_avg_raw * 0.44704, 1) if vel_avg_raw != 255 else None

        # Presion barométrica (1/1000 inHg)
        presion_raw   = struct.unpack('<H', data[14:16])[0]
        presion_hpa   = round(presion_raw * 0.033864, 1) if presion_raw != 0 else None

        # Temperatura interior (1/10 F)
        temp_int_raw  = struct.unpack('<h', data[16:18])[0]
        temp_int_c    = round((temp_int_raw / 10 - 32) * 5 / 9, 1) if temp_int_raw != 32767 else None

        # Humedad exterior (%)
        hum_ext       = data[18] if data[18] != 255 else None

        # Humedad interior (%)
        hum_int       = data[19] if data[19] != 255 else None

        # Dirección del viento dominante (código 0-15 → rosa)
        dir_codigo    = data[20] & 0x0F
        rosas = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
                 "S","SSO","SO","OSO","O","ONO","NO","NNO"]
        dir_rosa      = rosas[dir_codigo] if dir_codigo < 16 else "?"

        # Dirección del viento de ráfaga máxima (código 0-15)
        dir_hi_codigo = (data[20] >> 4) & 0x0F
        dir_hi_rosa   = rosas[dir_hi_codigo] if dir_hi_codigo < 16 else "?"

        # Radiación solar media (W/m²)
        solar_raw     = struct.unpack('<H', data[22:24])[0]
        solar_wm2     = solar_raw if solar_raw != 32767 else None

        # Índice UV medio (UV/10)
        uv_raw        = data[24]
        uv_index      = round(uv_raw / 10, 1) if uv_raw != 255 else None

        # ET acumulado (1/1000 in)
        et_raw        = data[25]
        et_mm         = round(et_raw * 0.0254, 3) if et_raw != 255 else None

        return {
            "fecha_hora":       fecha_hora,
            "temp_ext_c":       temp_ext_c,
            "temp_ext_hi_c":    temp_hi_c,
            "temp_ext_lo_c":    temp_lo_c,
            "temp_int_c":       temp_int_c,
            "hum_ext_pct":      hum_ext,
            "hum_int_pct":      hum_int,
            "presion_hpa":      presion_hpa,
            "vel_viento_ms":    vel_avg_ms,
            "vel_max_ms":       vel_hi_ms,
            "dir_viento":       dir_rosa,
            "dir_rafaga":       dir_hi_rosa,
            "lluvia_mm":        lluvia_mm,
            "solar_wm2":        solar_wm2,
            "uv_index":         uv_index,
            "et_mm":            et_mm,
        }
    except Exception as e:
        print(f"  [!] Error parseando registro: {e}")
        return None


# ──────────────────────────────────────────────────────────────────
#  DESCARGA DMP — TODOS LOS REGISTROS
# ──────────────────────────────────────────────────────────────────

def descargar_dmp(ser: serial.Serial) -> list:
    """
    Usa el comando DMP para descargar TODOS los registros del datalogger.
    Retorna lista de dicts con los registros parseados.
    """
    print("\n  Enviando comando DMP...")
    ser.reset_input_buffer()
    ser.write(b'DMP\n')
    time.sleep(1)

    # Esperar ACK
    ack = ser.read(1)
    if ack != ACK:
        print(f"  [!] ACK no recibido: {ack!r}")
        return []

    registros = []
    pagina_num = 0

    print("  Descargando páginas", end="", flush=True)

    while True:
        # Enviar ACK para recibir la siguiente página
        ser.write(ACK)
        time.sleep(0.5)

        # Leer 267 bytes (1 seq + 260 datos + 4 CRC de registros + 2 CRC página)
        pagina = ser.read(267)

        if len(pagina) < 4:
            # No hay más datos
            break

        # Byte 0: número de secuencia de página
        seq = pagina[0]

        # Verificar CRC de la página (últimos 2 bytes)
        if calcular_crc(pagina) != 0:
            print(f"\n  [!] CRC incorrecto en página {pagina_num}, reintentando...")
            ser.write(NAK)
            time.sleep(0.5)
            continue

        # Los 260 bytes del medio contienen 5 registros de 52 bytes
        datos_pagina = pagina[1:261]

        for i in range(REGISTROS_POR_PAGINA):
            offset  = i * BYTES_POR_REGISTRO
            reg_raw = datos_pagina[offset:offset + BYTES_POR_REGISTRO]
            reg     = parsear_registro(reg_raw)
            if reg is not None:
                registros.append(reg)

        pagina_num += 1
        print(".", end="", flush=True)

        # Si seq == 255 es la última página
        if seq == 0xFF or len(pagina) < 267:
            ser.write(ESC)   # indicar fin de descarga
            break

    print(f" {pagina_num} páginas recibidas")
    return registros


# ──────────────────────────────────────────────────────────────────
#  DESCARGA DMPAFT — REGISTROS DESDE UNA FECHA
# ──────────────────────────────────────────────────────────────────

def codificar_fecha_dmpaft(dt: datetime) -> bytes:
    """
    Codifica datetime en el formato de 6 bytes que espera DMPAFT:
    2 bytes fecha (mismo formato dateStamp) + 2 bytes hora + 2 bytes CRC
    """
    date_stamp = (dt.day) | (dt.month << 5) | ((dt.year - 2000) << 9)
    time_stamp = dt.hour * 100 + dt.minute
    data = struct.pack('<HH', date_stamp, time_stamp)
    crc  = calcular_crc(data)
    # CRC se envía MSB primero (segun manual Davis)
    return data + struct.pack('>H', crc)


def descargar_dmpaft(ser: serial.Serial, desde: datetime) -> list:
    """
    Descarga solo los registros posteriores a 'desde'.
    Más eficiente que DMP cuando solo se necesitan datos recientes.
    """
    print(f"\n  Enviando DMPAFT desde {desde.strftime('%Y-%m-%d %H:%M')}...")

    ser.reset_input_buffer()
    ser.write(b'DMPAFT\n')
    time.sleep(0.5)

    ack = ser.read(1)
    if ack != ACK:
        print(f"  [!] ACK no recibido al DMPAFT: {ack!r}")
        return []

    # Enviar fecha/hora codificada (6 bytes)
    payload = codificar_fecha_dmpaft(desde)
    ser.write(payload)
    time.sleep(1)

    # Recibir confirmación: ACK + 4 bytes (páginas totales + primer registro)
    respuesta = ser.read(6)
    if len(respuesta) < 5 or respuesta[0:1] != ACK:
        print(f"  [!] Respuesta inesperada al enviar fecha: {respuesta!r}")
        return []

    total_paginas   = struct.unpack('<H', respuesta[1:3])[0]
    primer_registro = struct.unpack('<H', respuesta[3:5])[0]

    print(f"  Páginas a descargar: {total_paginas}  |  Primer registro: {primer_registro}")

    registros = []
    pagina_num = 0
    primera_pagina = True

    print("  Descargando", end="", flush=True)

    while pagina_num < total_paginas:
        ser.write(ACK)
        time.sleep(0.5)

        pagina = ser.read(267)
        if len(pagina) < 4:
            break

        if calcular_crc(pagina) != 0:
            print(f"\n  [!] CRC incorrecto en página {pagina_num}, reintentando...")
            ser.write(NAK)
            time.sleep(0.5)
            continue

        datos_pagina = pagina[1:261]
        inicio = primer_registro if primera_pagina else 0
        primera_pagina = False

        for i in range(inicio, REGISTROS_POR_PAGINA):
            offset  = i * BYTES_POR_REGISTRO
            reg_raw = datos_pagina[offset:offset + BYTES_POR_REGISTRO]
            reg     = parsear_registro(reg_raw)
            if reg is not None:
                registros.append(reg)

        pagina_num += 1
        print(".", end="", flush=True)

    ser.write(ESC)
    print(f" {pagina_num} páginas recibidas")
    return registros


# ──────────────────────────────────────────────────────────────────
#  GUARDAR CSV
# ──────────────────────────────────────────────────────────────────

COLUMNAS = [
    "fecha_hora", "temp_ext_c", "temp_ext_hi_c", "temp_ext_lo_c",
    "temp_int_c", "hum_ext_pct", "hum_int_pct", "presion_hpa",
    "vel_viento_ms", "vel_max_ms", "dir_viento", "dir_rafaga",
    "lluvia_mm", "solar_wm2", "uv_index", "et_mm"
]

ENCABEZADOS = {
    "fecha_hora":    "Fecha y Hora",
    "temp_ext_c":    "Temp Ext (°C)",
    "temp_ext_hi_c": "Temp Ext Máx (°C)",
    "temp_ext_lo_c": "Temp Ext Mín (°C)",
    "temp_int_c":    "Temp Int (°C)",
    "hum_ext_pct":   "Hum Ext (%)",
    "hum_int_pct":   "Hum Int (%)",
    "presion_hpa":   "Presión (hPa)",
    "vel_viento_ms": "Vel Viento (m/s)",
    "vel_max_ms":    "Vel Máx (m/s)",
    "dir_viento":    "Dir Viento",
    "dir_rafaga":    "Dir Ráfaga",
    "lluvia_mm":     "Lluvia (mm)",
    "solar_wm2":     "Rad Solar (W/m²)",
    "uv_index":      "Índice UV",
    "et_mm":         "ET (mm)",
}


def guardar_csv(registros: list, archivo: str):
    modo    = 'a' if os.path.exists(archivo) else 'w'
    es_nuevo = modo == 'w'

    with open(archivo, modo, newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNAS)
        if es_nuevo:
            writer.writerow(ENCABEZADOS)   # fila de títulos legibles
            writer.writeheader()           # fila de claves (para uso programático)
        writer.writerows(registros)

    print(f"  Guardado en: {os.path.abspath(archivo)}")


def mostrar_resumen(registros: list):
    if not registros:
        print("  No se obtuvieron registros.")
        return

    print(f"\n  {'─'*54}")
    print(f"  RESUMEN DE DESCARGA")
    print(f"  {'─'*54}")
    print(f"  Total de registros  : {len(registros)}")
    fechas = [r['fecha_hora'] for r in registros if r['fecha_hora'] != 'Fecha inválida']
    if fechas:
        print(f"  Registro más antiguo: {min(fechas)}")
        print(f"  Registro más reciente: {max(fechas)}")

    # Mostrar los últimos 5 registros
    print(f"\n  Últimos 3 registros:")
    print(f"  {'Fecha/Hora':<18} {'T.Ext':>7} {'Hum':>5} {'Presión':>9} {'Viento':>8} {'Solar':>7}")
    print(f"  {'─'*18} {'─'*7} {'─'*5} {'─'*9} {'─'*8} {'─'*7}")
    for r in registros[-3:]:
        t   = str(r.get('temp_ext_c',   '--'))
        h   = str(r.get('hum_ext_pct',  '--'))
        p   = str(r.get('presion_hpa',  '--'))
        v   = str(r.get('vel_viento_ms','--'))
        s   = str(r.get('solar_wm2',    'S/S'))
        print(f"  {r['fecha_hora']:<18} {t:>6}C {h:>4}% {p:>8}hPa {v:>6}m/s {s:>6}")
    print(f"  {'─'*54}\n")


# ══════════════════════════════════════════════════════════════════
#  PROGRAMA PRINCIPAL
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":

    print("\n" + "=" * 56)
    print("  Davis Vantage Pro 2  —  Descarga de Datalogger")
    print("=" * 56)

    print("""
  Modos de descarga disponibles:
    [1] DMP     — Descargar TODOS los registros del datalogger
    [2] DMPAFT  — Descargar registros desde una fecha específica
    [3] DMPAFT  — Descargar registros de las últimas N horas
""")

    modo = input("  Selecciona modo (1/2/3): ").strip()

    desde_dt = None
    if modo == '2':
        fecha_str = input("  Fecha de inicio (YYYY-MM-DD HH:MM): ").strip()
        try:
            desde_dt = datetime.strptime(fecha_str, "%Y-%m-%d %H:%M")
        except ValueError:
            print("  [!] Formato de fecha incorrecto. Usando últimas 24 horas.")
            desde_dt = datetime.now() - timedelta(hours=24)
    elif modo == '3':
        horas = input("  ¿Cuántas horas atrás? (ej: 24): ").strip()
        try:
            desde_dt = datetime.now() - timedelta(hours=float(horas))
        except ValueError:
            desde_dt = datetime.now() - timedelta(hours=24)
        print(f"  Descargando desde: {desde_dt.strftime('%Y-%m-%d %H:%M')}")

    # Nombre del archivo de salida
    archivo_salida = input(f"\n  Nombre del CSV [{ARCHIVO_CSV}]: ").strip()
    if not archivo_salida:
        archivo_salida = ARCHIVO_CSV

    # Abrir puerto
    print(f"\n  Conectando a {SERIAL_PORT} @ {BAUD_RATE} baud...")
    try:
        ser = abrir_puerto()
        print("  Puerto abierto OK")
    except serial.SerialException as e:
        print(f"\n  [ERROR] {e}")
        exit(1)

    # Despertar consola
    if not despertar(ser):
        print("  [ERROR] La consola no responde.")
        ser.close()
        exit(1)

    # Leer info básica
    info = leer_info_consola(ser)
    print(f"  Firmware           : {info.get('firmware', '?')}")
    print(f"  Intervalo archivo  : {info.get('intervalo_min', '?')} minutos")

    # Descargar según modo
    try:
        if modo == '1':
            registros = descargar_dmp(ser)
        else:
            registros = descargar_dmpaft(ser, desde_dt)
    except Exception as e:
        print(f"\n  [ERROR durante descarga] {e}")
        registros = []
    finally:
        ser.close()
        print("  Puerto serial cerrado.")

    # Guardar y mostrar resultados
    if registros:
        guardar_csv(registros, archivo_salida)
        mostrar_resumen(registros)
        print(f"  Descarga completada: {len(registros)} registros en '{archivo_salida}'")
    else:
        print("\n  No se obtuvieron registros. Verifica:")
        print("   - Que el WeatherLink data logger esté conectado a la consola")
        print("   - Que el intervalo de archivo esté configurado (no en cero)")
        print("   - Que haya datos almacenados (la consola necesita tiempo de operación)")
