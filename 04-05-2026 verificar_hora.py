import serial
import struct
import time

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

def leer_hora_consola(ser):
    """
    Comando GETTIME: responde ACK + 6 bytes + 2 CRC
    Bytes: segundos, minutos, horas, dia, mes, año (año desde 1900)
    """
    ser.reset_input_buffer()
    ser.write(b'GETTIME\n')
    time.sleep(0.5)

    ack = ser.read(1)
    if ack != b'\x06':
        return None

    data = ser.read(8)   # 6 bytes de tiempo + 2 bytes CRC
    if len(data) < 6:
        return None

    seg  = data[0]
    min_ = data[1]
    hora = data[2]
    dia  = data[3]
    mes  = data[4]
    anio = data[5] + 1900

    return f"{anio:04d}-{mes:02d}-{dia:02d}  {hora:02d}:{min_:02d}:{seg:02d}"


# ── MAIN ─────────────────────────────────────
print("\n" + "="*46)
print("  Davis Vantage Pro 2  —  Verificación de hora")
print("="*46)

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
print(" OK")

hora_pc      = time.strftime('%Y-%m-%d  %H:%M:%S')
hora_consola = leer_hora_consola(ser)
ser.close()

print()
print(f"  Hora PC       : {hora_pc}")

if hora_consola:
    print(f"  Hora consola  : {hora_consola}")

    # Calcular diferencia en segundos
    from datetime import datetime
    fmt = "%Y-%m-%d  %H:%M:%S"
    t_pc  = datetime.strptime(hora_pc,      fmt)
    t_con = datetime.strptime(hora_consola, fmt)
    diff  = abs((t_pc - t_con).total_seconds())

    print()
    if diff == 0:
        print("  Resultado: SINCRONIZADAS exactamente ✓")
    elif diff <= 60:
        print(f"  Resultado: Diferencia de {diff:.0f} segundos  (aceptable)")
    else:
        mins = diff / 60
        print(f"  Resultado: Diferencia de {mins:.1f} minutos  [!] Revisar hora en consola")
else:
    print("  Hora consola  : [ERROR] No se pudo leer")

print()
