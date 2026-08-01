import serial
import struct
import time
from datetime import datetime, timezone

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

def sincronizar_hora_utc(ser) -> bool:
    """
    Envía SETTIME con la hora UTC actual del PC.
    Formato: comando -> ACK -> 6 bytes (seg,min,hora,dia,mes,año-1900) + 2 CRC -> ACK
    """
    # Tomar hora UTC justo antes de enviar para mayor precision
    ahora = datetime.now(timezone.utc)

    data = bytes([
        ahora.second,
        ahora.minute,
        ahora.hour,
        ahora.day,
        ahora.month,
        ahora.year - 1900
    ])

    crc       = calcular_crc(data)
    crc_bytes = struct.pack('>H', crc)   # MSB primero segun protocolo Davis

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
        print(f"  [!] ACK de confirmacion no recibido: {ack2!r}")
        return False

    return True


# ── MAIN ─────────────────────────────────────
print("\n" + "="*50)
print("  Davis Vantage Pro 2  --  Sincronizar hora (UTC)")
print("="*50)

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
hora_utc_antes     = datetime.now(timezone.utc).strftime('%Y-%m-%d  %H:%M:%S') + "  (UTC)"
hora_local_antes   = datetime.now().strftime('%H:%M:%S') + "  (hora local)"
hora_consola_antes = leer_hora_consola(ser)

print("  ANTES de sincronizar:")
print(f"    PC UTC        : {hora_utc_antes}")
print(f"    PC local      : {hora_local_antes}")
print(f"    Consola Davis : {hora_consola_antes if hora_consola_antes else '[error de lectura]'}")

# 2. Sincronizar
print("\n  Enviando hora UTC a la consola...", end="", flush=True)
if not despertar(ser):
    print(" FALLO al redespertar")
    ser.close()
    exit(1)

ok = sincronizar_hora_utc(ser)

if ok:
    print(" OK")
else:
    print(" FALLO")
    ser.close()
    exit(1)

# 3. Verificar hora DESPUES
time.sleep(1)
despertar(ser)
hora_utc_despues     = datetime.now(timezone.utc).strftime('%Y-%m-%d  %H:%M:%S') + "  (UTC)"
hora_consola_despues = leer_hora_consola(ser)
ser.close()

print(f"\n  DESPUES de sincronizar:")
print(f"    PC UTC        : {hora_utc_despues}")
print(f"    Consola Davis : {hora_consola_despues if hora_consola_despues else '[error de lectura]'}")

# 4. Resultado
if hora_consola_despues:
    fmt  = "%Y-%m-%d  %H:%M:%S"
    diff = abs((datetime.strptime(hora_utc_despues[:19], fmt) -
                datetime.strptime(hora_consola_despues,  fmt)).total_seconds())
    print()
    if diff <= 2:
        print("  Resultado: Sincronizacion UTC exitosa ✓")
    else:
        print(f"  Resultado: Diferencia de {diff:.0f}s -- ejecuta el script de nuevo")

print()
