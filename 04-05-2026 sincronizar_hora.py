import serial
import struct
import time
from datetime import datetime

# ══════════════════════════════════════════════
SERIAL_PORT = "COM3"
BAUD_RATE   = 19200
# ══════════════════════════════════════════════

def despertar(ser):
    for _ in range(3):
        ser.reset_input_buffer()
        ser.write(b'\n')
        time.sleep(1.2)
        r = ser.read(ser.in_waiting or 2)
        if b'\n\r' in r or b'\r\n' in r:
            return True
    return False

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

def leer_hora_consola(ser) -> str | None:
    """Lee la hora actual de la consola con GETTIME."""
    ser.reset_input_buffer()
    ser.write(b'GETTIME\n')
    time.sleep(0.5)
    ack = ser.read(1)
    if ack != b'\x06':
        return None
    data = ser.read(8)
    if len(data) < 6:
        return None
    seg  = data[0]
    min_ = data[1]
    hora = data[2]
    dia  = data[3]
    mes  = data[4]
    anio = data[5] + 1900
    return f"{anio:04d}-{mes:02d}-{dia:02d}  {hora:02d}:{min_:02d}:{seg:02d}"

def sincronizar_hora(ser) -> bool:
    """
    Envía SETTIME con la hora actual del PC.
    Formato: ACK + 6 bytes (seg, min, hora, dia, mes, año-1900) + 2 bytes CRC
    El CRC se envía MSB primero (segun manual Davis).
    """
    # Tomar la hora del PC justo antes de enviar para mayor precisión
    ahora = datetime.now()

    data = bytes([
        ahora.second,
        ahora.minute,
        ahora.hour,
        ahora.day,
        ahora.month,
        ahora.year - 1900
    ])

    crc = calcular_crc(data)
    crc_bytes = struct.pack('>H', crc)   # MSB primero

    # Secuencia: comando → esperar ACK → enviar 6 bytes + CRC → esperar ACK
    ser.reset_input_buffer()
    ser.write(b'SETTIME\n')
    time.sleep(0.5)

    ack1 = ser.read(1)
    if ack1 != b'\x06':
        print(f"  [!] ACK inicial no recibido: {ack1!r}")
        return False

    ser.write(data + crc_bytes)
    time.sleep(0.5)

    ack2 = ser.read(1)
    if ack2 != b'\x06':
        print(f"  [!] ACK de confirmación no recibido: {ack2!r}")
        return False

    return True


# ── MAIN ─────────────────────────────────────
print("\n" + "="*48)
print("  Davis Vantage Pro 2  —  Sincronizar hora")
print("="*48)

try:
    ser = serial.Serial(
        port=SERIAL_PORT, baudrate=BAUD_RATE,
        bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE, timeout=5
    )
except serial.SerialException as e:
    print(f"\n  [ERROR] No se pudo abrir {SERIAL_PORT}: {e}")
    exit(1)

print(f"  Puerto {SERIAL_PORT} abierto OK")
print("  Despertando consola...", end="", flush=True)

if not despertar(ser):
    print(" FALLO")
    ser.close()
    exit(1)
print(" OK\n")

# 1. Leer hora ANTES
hora_pc_antes    = time.strftime('%Y-%m-%d  %H:%M:%S')
hora_antes       = leer_hora_consola(ser)

print(f"  ANTES")
print(f"    Hora PC       : {hora_pc_antes}")
print(f"    Hora consola  : {hora_antes if hora_antes else '[error de lectura]'}")

# 2. Sincronizar
print("\n  Sincronizando...", end="", flush=True)
if not despertar(ser):
    print(" FALLO al redespertar")
    ser.close()
    exit(1)

ok = sincronizar_hora(ser)

if ok:
    print(" OK")
else:
    print(" FALLO")
    ser.close()
    exit(1)

# 3. Verificar hora DESPUÉS
time.sleep(1)
despertar(ser)
hora_pc_despues = time.strftime('%Y-%m-%d  %H:%M:%S')
hora_despues    = leer_hora_consola(ser)
ser.close()

print(f"\n  DESPUES")
print(f"    Hora PC       : {hora_pc_despues}")
print(f"    Hora consola  : {hora_despues if hora_despues else '[error de lectura]'}")

# 4. Calcular diferencia final
if hora_despues:
    fmt   = "%Y-%m-%d  %H:%M:%S"
    diff  = abs((datetime.strptime(hora_pc_despues, fmt) -
                 datetime.strptime(hora_despues,    fmt)).total_seconds())
    print()
    if diff <= 2:
        print("  Resultado: Sincronización exitosa ✓")
    else:
        print(f"  Resultado: Diferencia de {diff:.0f}s — intenta ejecutarlo de nuevo")

print()
