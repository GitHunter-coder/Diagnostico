import os
import sys
import ctypes
import subprocess
import time
import random
import json
import urllib.request
import re
import shutil
import struct
import platform
import uuid
import getpass
import hashlib
import glob
import tempfile
import socket
import ipaddress
import threading
import secrets
import traceback
import contextlib
import webbrowser
import http.server
import urllib.parse
from datetime import datetime, timedelta

# ==========================================
# CONFIGURACIÓN DE UMBRALES DE RIESGO
# ==========================================
UMBRAL_TEMP_CRITICA = 70  # °C
UMBRAL_DESGASTE_CRITICO = 90  # %
UMBRAL_DESGASTE_ADVERTENCIA = 70  # %
UMBRAL_USO_CRITICO = 90  # % de capacidad utilizada
VERSION_SCRIPT = "V3"
ES_WINDOWS = os.name == "nt"
SISTEMA = platform.system()          # 'Windows' | 'Linux' | 'Darwin'
SMART_AUTORIZADO = True              # el modo cliente lo desactiva salvo --smart (consentimiento explícito)
TAM_SEQ_SSD_MB, TAM_SEQ_HDD_MB = 2048, 500
ORIGENES_WEB_DEFECTO = []            # el constructor del sitio escribe aquí el origen de GitHub Pages
URL_SITIO_DEFECTO = ""

def escribir_log_depuracion(mensaje):
    """Escribe un registro detallado en el archivo de depuración local."""
    log_path = os.path.join(carpeta_programa(), "smartctl_download_debug.log")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entrada = f"[{timestamp}] {mensaje}\n"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entrada)
    except Exception:
        pass
    print(f"[DEBUG LOG] {mensaje}")

def es_administrador():
    """Verifica si el script se ejecuta como Administrador (Windows) o root (Linux/macOS)."""
    if not ES_WINDOWS:
        try:
            return os.geteuid() == 0
        except AttributeError:
            return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

EMPAQUETADO = bool(getattr(sys, "frozen", False))      # True cuando corre como .exe (PyInstaller)

def carpeta_programa():
    """Carpeta donde viven el programa y sus datos (junto al .exe si está empaquetado; en el .exe, __file__ apunta a una carpeta temporal)."""
    return os.path.dirname(os.path.abspath(sys.executable if EMPAQUETADO else __file__))

def comando_programa():
    """Partes del comando que vuelve a ejecutar este mismo programa."""
    return [sys.executable] if EMPAQUETADO else [sys.executable, os.path.abspath(sys.argv[0])]

def elevar_privilegios():
    """Solicita elevación UAC automáticamente (solo Windows)."""
    if not ES_WINDOWS:
        return
    try:
        partes = comando_programa()
        params = ' '.join([f'"{x}"' for x in partes[1:] + sys.argv[1:]])
        ctypes.windll.shell32.ShellExecuteW(None, "runas", partes[0], params, None, 1)
    except Exception as e:
        escribir_log_depuracion(f"ERROR: No se pudo elevar privilegios: {e}")
    sys.exit()

def ejecutar_comando_powershell(script_ps):
    """Ejecuta comandos de PowerShell de forma segura y devuelve la salida."""
    if not ES_WINDOWS:
        return ""
    cmd = ["powershell", "-NoProfile", "-Command", script_ps]
    proceso = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    return proceso.stdout.strip()

# IDs de atributos SMART SATA cuyo valor normalizado indica vida restante (100 = nuevo)
_ATA_IDS_VIDA_RESTANTE = (177, 231, 233, 202)

def preparar_smartctl(directorio_actual):
    """smartctl es OPCIONAL: solo se usa si ya existe. No se descarga nada."""
    for ruta in (os.path.join(directorio_actual, "smartctl.exe"), _buscar_smartctl_instalado()):
        if ruta and os.path.exists(ruta):
            escribir_log_depuracion(f"smartctl opcional detectado: {ruta}")
            return ruta
    escribir_log_depuracion("smartctl no presente: se usa el motor nativo de Windows (sin dependencias).")
    return None

def _buscar_smartctl_instalado():
    candidatos = [
        shutil.which("smartctl"),
        r"C:\Program Files\smartmontools\bin\smartctl.exe",
        r"C:\Program Files (x86)\smartmontools\bin\smartctl.exe",
    ]
    return next((c for c in candidatos if c and os.path.exists(c)), None)

# ==========================================
# MOTOR NATIVO (sin dependencias externas)
# ==========================================
IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400

def _parsear_nvme_health(b):
    """Interpreta el log NVMe 02h (SMART / Health Information, 512 bytes)."""
    if len(b) < 192:
        return None
    u128 = lambda o: int.from_bytes(b[o:o + 16], "little")
    kelvin = int.from_bytes(b[1:3], "little")
    return {
        "critical_warning": b[0],
        "temperature": kelvin - 273 if kelvin else None,
        "available_spare": b[3],
        "available_spare_threshold": b[4],
        "percentage_used": b[5],
        "data_units_read": u128(32),
        "data_units_written": u128(48),
        "power_cycles": u128(112),
        "power_on_hours": u128(128),
        "unsafe_shutdowns": u128(144),
        "media_errors": u128(160),
    }

_NVME_VARIANTE_OK = None

def _leer_nvme_health_nativo(disk_num):
    global _NVME_VARIANTE_OK
    """Lee el log SMART de un NVMe vía DeviceIoControl. Requiere Administrador."""
    if os.name != "nt":
        return None
    try:
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        k32.DeviceIoControl.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                        ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]

        h = k32.CreateFileW(rf"\\.\PhysicalDrive{disk_num}", 0xC0000000, 3, None, 3, 0, None)
        if h is None or h == ctypes.c_void_p(-1).value:
            escribir_log_depuracion(f"NVMe nativo: no se pudo abrir PhysicalDrive{disk_num} (error {ctypes.get_last_error()})")
            return None
        try:
            # STORAGE_PROPERTY_QUERY = PropertyId(4) + QueryType(4); AdditionalParameters empieza en el byte 8
            # y es donde va STORAGE_PROTOCOL_SPECIFIC_DATA (28 bytes; 40 en SDK nuevos). Probamos variantes.
            variantes = [(50, 40), (50, 28), (49, 40), (49, 28)]
            if _NVME_VARIANTE_OK in variantes:  # reutiliza la variante que ya funcionó
                variantes.remove(_NVME_VARIANTE_OK)
                variantes.insert(0, _NVME_VARIANTE_OK)
            for prop_id, tam_proto in variantes:
                campos = [3, 2, 2, 0, tam_proto, 512, 0] + [0] * ((tam_proto - 28) // 4)
                entrada = (struct.pack("<II", prop_id, 0)
                           + struct.pack(f"<{len(campos)}I", *campos) + b"\x00" * 512)
                buf = (ctypes.c_char * len(entrada)).from_buffer_copy(entrada)  # sin byte extra
                devuelto = wintypes.DWORD(0)
                ok = k32.DeviceIoControl(h, IOCTL_STORAGE_QUERY_PROPERTY, buf, len(entrada), buf, len(entrada),
                                         ctypes.byref(devuelto), None)
                if not ok:
                    escribir_log_depuracion(f"NVMe nativo: variante prop={prop_id}, estructura={tam_proto}B falló (error {ctypes.get_last_error()})")
                    continue
                crudo = bytes(buf)
                offset_datos = struct.unpack_from("<I", crudo, 8 + 16)[0]  # ProtocolDataOffset
                nv = _parsear_nvme_health(crudo[8 + offset_datos: 8 + offset_datos + 512])
                if nv:
                    if _NVME_VARIANTE_OK != (prop_id, tam_proto):
                        escribir_log_depuracion(f"NVMe nativo: OK con prop={prop_id}, estructura={tam_proto}B")
                    _NVME_VARIANTE_OK = (prop_id, tam_proto)
                    return nv
            return None
        finally:
            k32.CloseHandle(h)
    except Exception as e:
        escribir_log_depuracion(f"NVMe nativo: excepción {e!r}")
        return None

def _parsear_atributos_ata(b):
    """Interpreta VendorSpecific de MSStorageDriver_FailurePredictData (30 entradas de 12 bytes)."""
    attrs = {}
    for i in range(30):
        o = 2 + i * 12
        if o + 12 > len(b) or b[o] == 0:
            continue
        attrs[b[o]] = {"value": b[o + 3], "worst": b[o + 4],
                       "raw": int.from_bytes(b[o + 5:o + 11], "little")}
    return attrs

def _leer_atributos_ata_wmi(disk_num):
    """Tabla SMART ATA (SATA) mediante WMI nativo; None si el disco no la expone (p. ej. NVMe)."""
    ps = """
    $ErrorActionPreference = 'SilentlyContinue'
    $d = Get-CimInstance Win32_DiskDrive -Filter "Index=NUM"
    $p = Get-CimInstance -Namespace root\\wmi -ClassName MSStorageDriver_FailurePredictData |
         Where-Object { $_.InstanceName -like ($d.PNPDeviceID + '*') } | Select-Object -First 1
    if ($p) { ($p.VendorSpecific | ForEach-Object { '{0:X2}' -f $_ }) -join '' }
    """.replace("NUM", str(int(disk_num)))
    hexa = ejecutar_comando_powershell(ps).strip()
    if len(hexa) < 26 or not re.fullmatch(r"[0-9A-Fa-f]+", hexa):
        return None
    return _parsear_atributos_ata(bytes.fromhex(hexa))

def _aplicar_motor_nativo(datos, disk_num):
    """Completa 'datos' con NVMe (IOCTL) o atributos ATA (WMI). Devuelve True si obtuvo algo."""
    gb = lambda unidades: f"{unidades * 512000 / 1e9:.1f} GB"
    if datos["tecnologia"] == "NVMe":
        nv = _leer_nvme_health_nativo(disk_num)
        if nv:
            if nv["temperature"] is not None:
                datos["temp"] = str(nv["temperature"])
            datos["power_hours"] = nv["power_on_hours"]
            datos["fecha_inicio"] = (datetime.now() - timedelta(hours=nv["power_on_hours"])).strftime("%Y-%m-%d")
            datos["wear"] = str(min(100, nv["percentage_used"]))
            datos["total_lecturas"] = gb(nv["data_units_read"])
            datos["total_escrituras"] = gb(nv["data_units_written"])
            datos["r_err"] = str(nv["media_errors"])
            datos["fuente"] = "DeviceIoControl NVMe Log 02h (nativo)"
            datos["extra"] = {k: nv[k] for k in ("critical_warning", "available_spare", "available_spare_threshold",
                                                 "power_cycles", "unsafe_shutdowns", "media_errors")}
            datos["smart_json_raw"] = json.dumps({"origen": "DeviceIoControl NVMe Log 02h",
                                                  "nvme_smart_health_information_log": nv}, indent=4)
            return True
        return False

    attrs = _leer_atributos_ata_wmi(disk_num)
    if not attrs:
        return False
    for aid in (194, 190):
        if aid in attrs:
            datos["temp"] = str(attrs[aid]["raw"] & 0xFF)
            break
    if 9 in attrs:
        h = attrs[9]["raw"] & 0xFFFFFFFF
        datos["power_hours"] = h
        datos["fecha_inicio"] = (datetime.now() - timedelta(hours=h)).strftime("%Y-%m-%d")
    for aid in _ATA_IDS_VIDA_RESTANTE:
        if aid in attrs:
            datos["wear"] = str(min(100, max(0, 100 - attrs[aid]["value"])))
            break
    if 241 in attrs:
        datos["total_escrituras"] = f"{attrs[241]['raw'] * 512 / 1e9:.1f} GB"
    if 242 in attrs:
        datos["total_lecturas"] = f"{attrs[242]['raw'] * 512 / 1e9:.1f} GB"
    datos["fuente"] = "WMI MSStorageDriver_FailurePredictData (nativo)"
    for aid, clave in ((5, "reallocated_sectors"), (197, "pending_sectors"), (198, "uncorrectable_sectors"),
                       (199, "crc_errors"), (12, "power_cycle_count")):
        if aid in attrs:
            datos["extra"][clave] = attrs[aid]["raw"] & 0xFFFFFFFF
    datos["smart_json_raw"] = json.dumps({
        "origen": "WMI MSStorageDriver_FailurePredictData",
        "ata_smart_attributes": [{"id": k, **v} for k, v in sorted(attrs.items())]}, indent=4)
    return True

# ==========================================
# CAPA MULTIPLATAFORMA (Linux / macOS)
# ==========================================
def ruta_raiz(unidad):
    """Raíz del volumen: 'C:\\' en Windows; el punto de montaje en Linux/macOS."""
    return unidad.rstrip(":") + ":\\" if ES_WINDOWS else unidad

def raiz_analisis(unidad):
    """Carpeta desde la que se recorre el volumen para clasificar archivos."""
    if SISTEMA == "Darwin" and unidad == "/" and os.path.isdir("/System/Volumes/Data"):
        return "/System/Volumes/Data"       # allí viven los datos del usuario y las aplicaciones
    return ruta_raiz(unidad)

def ruta_archivo_prueba(unidad):
    """Archivo temporal de benchmark en el MISMO dispositivo que la unidad (en POSIX busca una carpeta escribible)."""
    if ES_WINDOWS:
        return f"{unidad.rstrip(':')}:\\storage_benchmark_test.tmp"
    try:
        dev = os.stat(unidad).st_dev
    except OSError:
        dev = None
    for c in (unidad, os.path.join(unidad, "tmp"), os.path.expanduser("~"), tempfile.gettempdir()):
        try:
            if os.path.isdir(c) and os.access(c, os.W_OK) and (dev is None or os.stat(c).st_dev == dev):
                return os.path.join(c, ".storage_benchmark_test.tmp")
        except OSError:
            continue
    return os.path.join(tempfile.gettempdir(), ".storage_benchmark_test.tmp")

def _sin_cache(f):
    """Pide al SO que no sirva la lectura desde la caché de páginas (Linux); mejora la fidelidad del test de lectura."""
    try:
        os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    except (AttributeError, OSError):
        pass

def dispositivo_smartctl(disk_ref):
    return f"/dev/pd{disk_ref}" if ES_WINDOWS else f"/dev/{disk_ref}"

def _ejecutar(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""

def _disco_base(dispositivo):
    """nvme0n1p2 -> nvme0n1 ; sda1 -> sda ; mmcblk0p1 -> mmcblk0 ; disk3s1 -> disk3"""
    n = os.path.basename(dispositivo)
    for patron in (r"^(nvme\d+n\d+)p\d+$", r"^(mmcblk\d+)p\d+$", r"^(disk\d+)s\d+$", r"^([a-z]+)\d+$"):
        m = re.match(patron, n)
        if m:
            return m.group(1)
    return n

def _unidades_linux():
    ignorar = ("/snap", "/boot/efi", "/var/lib/docker", "/run", "/sys", "/proc", "/dev")
    unidades = {}
    crudo = _ejecutar(["lsblk", "-J", "-b", "-o", "NAME,KNAME,TYPE,MOUNTPOINTS,FSTYPE,RM"]) or \
            _ejecutar(["lsblk", "-J", "-b", "-o", "NAME,KNAME,TYPE,MOUNTPOINT,FSTYPE,RM"])
    try:
        arbol = json.loads(crudo).get("blockdevices", [])
    except (ValueError, AttributeError):
        arbol = []

    def visitar(nodo, disco, removible):
        tipo = nodo.get("type")
        if tipo == "loop":
            return
        if tipo == "disk":
            disco = nodo.get("kname") or nodo.get("name")
            removible = str(nodo.get("rm")).lower() in ("1", "true")
        puntos = nodo.get("mountpoints") or [nodo.get("mountpoint")]
        for mp in puntos:
            if mp and mp.startswith("/") and not mp.startswith(ignorar) and disco and not removible \
                    and nodo.get("fstype") not in ("squashfs", "swap"):
                unidades.setdefault(mp, disco)
        for hijo in nodo.get("children", []) or []:
            visitar(hijo, disco, removible)

    for n in arbol:
        visitar(n, None, False)
    if not unidades:    # respaldo: /proc/mounts
        try:
            with open("/proc/mounts", encoding="utf-8") as f:
                for linea in f:
                    src, mp, fs = linea.split()[:3]
                    if src.startswith("/dev/") and not src.startswith("/dev/loop") and not mp.startswith(ignorar) \
                            and fs not in ("squashfs", "swap"):
                        unidades.setdefault(mp.replace("\\040", " "), _disco_base(src))
        except OSError:
            pass
    return sorted(unidades.items(), key=lambda kv: (kv[0] != "/", kv[0]))

def _unidades_macos():
    try:
        import plistlib
        info = plistlib.loads(subprocess.run(["diskutil", "info", "-plist", "/"], capture_output=True, timeout=20).stdout)
        almacenes = info.get("APFSPhysicalStores") or []
        ref = (almacenes[0].get("APFSPhysicalStore") if almacenes else None) or info.get("ParentWholeDisk") or "disk0"
        return [("/", _disco_base(ref))]
    except Exception as e:
        escribir_log_depuracion(f"macOS: no se pudo leer el disco principal: {e!r}")
        return [("/", "disk0")]

def _unidades_posix():
    return _unidades_macos() if SISTEMA == "Darwin" else _unidades_linux()

def _leer_texto(ruta):
    try:
        with open(ruta, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""

def _temperatura_hwmon(disco):
    """Temperatura desde /sys/class/hwmon (nvme / drivetemp); no requiere root."""
    m = re.match(r"^(nvme\d+)", disco)
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        nombre = _leer_texto(os.path.join(hw, "name"))
        if nombre not in ("nvme", "drivetemp"):
            continue
        destino = os.path.realpath(os.path.join(hw, "device"))
        if (m and m.group(1) in destino) or (not m and f"/{disco}" in destino):
            t = _leer_texto(os.path.join(hw, "temp1_input"))
            if t.lstrip("-").isdigit():
                return int(t) // 1000
    return None

def _leer_nvme_health_linux(disco):
    """Log SMART de un NVMe vía ioctl NVME_IOCTL_ADMIN_CMD sobre /dev/nvmeX (requiere root)."""
    m = re.match(r"^(nvme\d+)n\d+", disco)
    if not m:
        return None
    try:
        import fcntl
        buf = ctypes.create_string_buffer(512)
        numd = 512 // 4 - 1
        cmd = bytearray(struct.pack("<BBHIIIQQ" + "I" * 10, 0x02, 0, 0, 0xFFFFFFFF, 0, 0, 0, ctypes.addressof(buf),
                                    0, 512, (numd << 16) | 0x02, 0, 0, 0, 0, 0, 0, 0))
        fd = os.open(f"/dev/{m.group(1)}", os.O_RDONLY)
        try:
            fcntl.ioctl(fd, 0xC0484E41, cmd)       # _IOWR('N', 0x41, struct nvme_admin_cmd[72 B])
        finally:
            os.close(fd)
        return _parsear_nvme_health(bytes(buf.raw))
    except Exception as e:
        escribir_log_depuracion(f"NVMe (Linux): no se pudo leer el log SMART de {disco}: {e!r}")
        return None

def _info_basica_posix(datos, disco):
    if SISTEMA == "Linux":
        modelo = _leer_texto(f"/sys/block/{disco}/device/model")
        if modelo:
            datos["modelo"] = modelo
        if disco.startswith("nvme"):
            datos["tecnologia"] = "NVMe"
        else:
            datos["tecnologia"] = "HDD" if _leer_texto(f"/sys/block/{disco}/queue/rotational") == "1" else "SSD"
    elif SISTEMA == "Darwin":
        try:
            import plistlib
            i = plistlib.loads(subprocess.run(["diskutil", "info", "-plist", f"/dev/{disco}"], capture_output=True, timeout=20).stdout)
            datos["modelo"] = i.get("MediaName") or i.get("IORegistryEntryName") or datos["modelo"]
            if "PCI" in str(i.get("BusProtocol", "")):
                datos["tecnologia"] = "NVMe"
            else:
                datos["tecnologia"] = "SSD" if i.get("SolidState") else "HDD"
            datos["extra_mac"] = i.get("SMARTStatus")
        except Exception as e:
            escribir_log_depuracion(f"macOS: info del disco {disco}: {e!r}")

def _smart_posix(datos, disco):
    """SMART nativo en Linux/macOS con lo que el sistema permite sin dependencias externas."""
    if SISTEMA == "Linux":
        t = _temperatura_hwmon(disco)
        if t is not None:
            datos["temp"] = str(t)
            datos["fuente"] = "sysfs/hwmon (Linux)"
        if es_administrador():
            nv = _leer_nvme_health_linux(disco)
            if nv:
                if nv["temperature"] is not None:
                    datos["temp"] = str(nv["temperature"])
                datos["power_hours"] = nv["power_on_hours"]
                datos["fecha_inicio"] = (datetime.now() - timedelta(hours=nv["power_on_hours"])).strftime("%Y-%m-%d")
                datos["wear"] = str(min(100, nv["percentage_used"]))
                datos["total_lecturas"] = f"{nv['data_units_read'] * 512000 / 1e9:.1f} GB"
                datos["total_escrituras"] = f"{nv['data_units_written'] * 512000 / 1e9:.1f} GB"
                datos["r_err"] = str(nv["media_errors"])
                datos["extra"] = {k: nv[k] for k in ("critical_warning", "available_spare", "available_spare_threshold",
                                                     "power_cycles", "unsafe_shutdowns", "media_errors")}
                datos["fuente"] = "NVMe admin ioctl (Linux)"
                datos["smart_json_raw"] = json.dumps({"origen": "NVME_IOCTL_ADMIN_CMD Log 02h",
                                                      "nvme_smart_health_information_log": nv}, indent=4)
        if datos["fuente"].startswith("Información"):
            datos["fuente"] = "sysfs (sin SMART detallado: ejecuta con sudo o instala smartmontools)"
    elif SISTEMA == "Darwin":
        estado = datos.get("extra_mac")
        if estado:
            datos["extra"]["smart_status"] = estado
            datos["fuente"] = "diskutil (estado SMART)"

def _inventario_posix(disco, unidad):
    base = {"serial": "N/D", "firmware": "N/D", "bus": "N/D", "media": "N/D", "salud": "N/D", "operacional": "N/D",
            "particion": "N/D", "fs": "N/D", "etiqueta": "", "salud_volumen": "N/D", "bitlocker": "No detectado", "disco_gb": None}
    try:
        if SISTEMA == "Linux":
            salida = _ejecutar(["lsblk", "-J", "-b", "-d", "-o", "NAME,SERIAL,MODEL,TRAN,ROTA,SIZE,PTTYPE", f"/dev/{disco}"])
            d = (json.loads(salida).get("blockdevices") or [{}])[0] if salida else {}
            base.update({"serial": d.get("serial") or "N/D", "bus": (d.get("tran") or ("nvme" if disco.startswith("nvme") else "N/D")).upper(),
                         "media": "HDD" if str(d.get("rota")).lower() in ("1", "true") else "SSD",
                         "particion": (d.get("pttype") or "N/D").upper(),
                         "disco_gb": round(int(d["size"]) / 1024 ** 3, 2) if d.get("size") else None,
                         "firmware": _leer_texto(f"/sys/block/{disco}/device/firmware_rev") or "N/D"})
            fs = _ejecutar(["findmnt", "-no", "FSTYPE,LABEL", unidad]).strip().split(None, 1)
            if fs:
                base["fs"] = fs[0]
                base["etiqueta"] = fs[1] if len(fs) > 1 else ""
            arbol = _ejecutar(["lsblk", "-J", "-o", "NAME,TYPE", f"/dev/{disco}"])
            base["bitlocker"] = "Activado (LUKS)" if '"crypt"' in arbol else "No detectado"
        elif SISTEMA == "Darwin":
            import plistlib
            i = plistlib.loads(subprocess.run(["diskutil", "info", "-plist", "/"], capture_output=True, timeout=20).stdout)
            base.update({"fs": i.get("FilesystemType") or "N/D", "etiqueta": i.get("VolumeName") or "",
                         "media": "SSD" if i.get("SolidState") else "HDD", "particion": "GPT",
                         "salud": {"Verified": "Healthy", "Failing": "Warning"}.get(i.get("SMARTStatus"), "N/D")})
            fv = _ejecutar(["fdesetup", "status"])
            base["bitlocker"] = "Activado (FileVault)" if "On" in fv else ("Desactivado" if "Off" in fv else "No detectado")
    except Exception as e:
        escribir_log_depuracion(f"Inventario POSIX de {unidad}: {e!r}")
    return base

def obtener_unidades_y_discos_fisicos():
    """Obtiene un mapeo de las unidades lógicas fijas y su disco físico (número en Windows, nombre en Linux/macOS)."""
    if not ES_WINDOWS:
        return _unidades_posix()
    script = """
    Get-CimInstance -ClassName Win32_LogicalDisk | Where-Object {$_.DriveType -eq 3} | ForEach-Object {
        $letra = $_.DeviceID
        $part = Get-Partition -DriveLetter $letra.Substring(0,1) -ErrorAction SilentlyContinue
        $diskNum = if ($part) { $part.DiskNumber } else { -1 }
        "$letra|$diskNum"
    }
    """
    salida = ejecutar_comando_powershell(script)
    mapeo = []
    for linea in salida.splitlines():
        if "|" in linea:
            letra, disk_num = linea.split("|")
            mapeo.append((letra.strip(), int(disk_num)))
    return mapeo


def _a_entero(valor, defecto=0):
    try:
        return int(float(valor))
    except (TypeError, ValueError):
        return defecto

def _consultar_smartctl(smartctl_bin, disk_num):
    """Prueba autodetección, NVMe y SAT; devuelve el primer JSON válido sin errores."""
    for extra in ([], ["-d", "nvme"], ["-d", "sat"]):
        cmd = [smartctl_bin, "-a", "-j", *extra, dispositivo_smartctl(disk_num)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=20)
        except Exception as e:
            escribir_log_depuracion(f"smartctl falló al ejecutarse ({' '.join(cmd)}): {e!r}")
            continue
        escribir_log_depuracion(f"{' '.join(cmd)} -> rc={r.returncode}")
        try:
            j = json.loads(r.stdout)
        except json.JSONDecodeError:
            escribir_log_depuracion(f"Sin JSON válido. stderr={r.stderr.strip()[:200]}")
            continue
        msgs = j.get("smartctl", {}).get("messages", [])
        if any(m.get("severity") == "error" for m in msgs):
            escribir_log_depuracion(f"smartctl reportó error: {msgs}")
            continue
        return r.stdout, j
    return None, None

def extraer_smart_profundo(smartctl_bin, disk_num, letra_unidad):
    """Extrae métricas mediante smartctl o utiliza el motor WMI de respaldo de Windows."""
    datos = {
        "modelo": "Desconocido", "tecnologia": "NVMe", "wear": "0",
        "temp": "38", "power_hours": 0, "fecha_inicio": "N/A",
        "total_lecturas": "N/A", "total_escrituras": "N/A", "r_err": "0", "w_err": "0",
        "extra": {}, "fuente": "Contador de confiabilidad de Windows (respaldo)", "sin_smart": not SMART_AUTORIZADO,
        "smart_json_raw": "{ \"aviso\": \"Modo de diagnóstico basado en subsistema nativo de Windows y contadores de rendimiento.\" }"
    }

    script_info = f"""
    $ErrorActionPreference = 'SilentlyContinue'
    $disk = Get-PhysicalDisk | Where-Object {{ $_.DeviceId -eq {disk_num} }}
    $modelo = if ($disk.FriendlyName) {{ $disk.FriendlyName }} else {{ 'Desconocido' }}
    $tecnologia = if ($disk.BusType -eq 'NVMe') {{ 'NVMe' }} elseif ($disk.MediaType -eq 'SSD') {{ 'SSD' }} else {{ 'HDD' }}

    $rel = $disk | Get-StorageReliabilityCounter
    $t = if ($rel.Temperature) {{ $rel.Temperature }} else {{ '38' }}
    $h = if ($rel.PowerOnHours) {{ $rel.PowerOnHours }} else {{ 0 }}
    $w = if ($rel.Wear) {{ $rel.Wear }} else {{ '0' }}
    $rErr = if ($rel.ReadErrorsTotal) {{ $rel.ReadErrorsTotal }} else {{ '0' }}
    $wErr = if ($rel.WriteErrorsTotal) {{ $rel.WriteErrorsTotal }} else {{ '0' }}

    "$modelo|$tecnologia|$t|$h|$w|$rErr|$wErr"
    """

    if not SMART_AUTORIZADO:      # sin consentimiento: solo identidad del disco, nada de contadores SMART
        script_info = script_info.replace("$rel = $disk | Get-StorageReliabilityCounter", "$rel = $null")
        datos["fuente"] = "SMART no autorizado por el usuario"
    elif not ES_WINDOWS:
        datos["fuente"] = "Información del sistema (sin SMART)"
    if not ES_WINDOWS:
        _info_basica_posix(datos, disk_num)
    res_info = ejecutar_comando_powershell(script_info)
    if res_info:
        partes = (res_info.split("|") + ["N/A"] * 7)[:7]  # protege contra campos faltantes
        datos["modelo"] = partes[0] or "Desconocido"
        datos["tecnologia"] = partes[1] if partes[1] in ("NVMe", "SSD", "HDD") else "NVMe"
        datos["temp"] = partes[2] if partes[2].isdigit() else "38"

        horas_h = _a_entero(partes[3], -1)
        if horas_h >= 0:
            datos["power_hours"] = horas_h
            datos["fecha_inicio"] = (datetime.now() - timedelta(hours=horas_h)).strftime("%Y-%m-%d")

        datos["wear"] = partes[4] if partes[4].isdigit() else "0"
        datos["r_err"] = partes[5] if partes[5].isdigit() else "0"
        datos["w_err"] = partes[6] if partes[6].isdigit() else "0"

        datos["smart_json_raw"] = json.dumps({
            "Dispositivo_Fisico": disk_num,
            "Modelo": datos["modelo"],
            "Tecnologia": datos["tecnologia"],
            "Temperatura_Actual_C": datos["temp"],
            "Horas_Encendido": datos["power_hours"],
            "Nivel_Desgaste_Porc": datos["wear"],
            "Errores_Lectura_Total": datos["r_err"],
            "Errores_Escritura_Total": datos["w_err"]
        }, indent=4)

    if SMART_AUTORIZADO:
        if ES_WINDOWS:
            _aplicar_motor_nativo(datos, disk_num)
        else:
            _smart_posix(datos, disk_num)

    if SMART_AUTORIZADO and smartctl_bin and os.path.exists(smartctl_bin):
        raw, j = _consultar_smartctl(smartctl_bin, disk_num)
        if j:
            datos["smart_json_raw"] = raw
            datos["fuente"] = "smartctl"
            try:
                if j.get("model_name"):
                    datos["modelo"] = j["model_name"]
                if "nvme_smart_health_information_log" in j:
                    datos["tecnologia"] = "NVMe"
                elif j.get("rotation_rate", 0) == 0 and "ata_smart_attributes" in j:
                    datos["tecnologia"] = "SSD"
                elif j.get("rotation_rate", 0) > 0:
                    datos["tecnologia"] = "HDD"

                temp = j.get("temperature", {}).get("current")
                if temp is not None:
                    datos["temp"] = str(temp)

                horas = j.get("power_on_time", {}).get("hours")
                if horas is not None:
                    datos["power_hours"] = int(horas)
                    datos["fecha_inicio"] = (datetime.now() - timedelta(hours=int(horas))).strftime("%Y-%m-%d")

                nvme_log = j.get("nvme_smart_health_information_log")
                if nvme_log:
                    for k in ("critical_warning", "available_spare", "available_spare_threshold",
                              "power_cycles", "unsafe_shutdowns", "media_errors"):
                        if k in nvme_log:
                            datos["extra"][k] = nvme_log[k]
                    if "percentage_used" in nvme_log:
                        datos["wear"] = str(min(100, max(0, _a_entero(nvme_log["percentage_used"]))))
                    # unidades de 512.000 bytes -> GB
                    if "data_units_read" in nvme_log:
                        datos["total_lecturas"] = f"{nvme_log['data_units_read'] * 512000 / 1e9:.1f} GB"
                    if "data_units_written" in nvme_log:
                        datos["total_escrituras"] = f"{nvme_log['data_units_written'] * 512000 / 1e9:.1f} GB"
                    if "media_errors" in nvme_log:
                        datos["r_err"] = str(nvme_log["media_errors"])
                else:
                    tabla = j.get("ata_smart_attributes", {}).get("table", [])
                    for attr in tabla:
                        aid = attr.get("id")
                        clave_extra = {5: "reallocated_sectors", 197: "pending_sectors", 198: "uncorrectable_sectors",
                                       199: "crc_errors", 12: "power_cycle_count"}.get(aid)
                        if clave_extra and attr.get("raw", {}).get("value") is not None:
                            datos["extra"][clave_extra] = attr["raw"]["value"] & 0xFFFFFFFF
                        if aid in _ATA_IDS_VIDA_RESTANTE and datos["wear"] in ("0", ""):
                            datos["wear"] = str(min(100, max(0, 100 - _a_entero(attr.get("value"), 100))))
                        elif aid == 241 and attr.get("raw", {}).get("value") is not None:  # Total_LBAs_Written
                            datos["total_escrituras"] = f"{attr['raw']['value'] * 512 / 1e9:.1f} GB"
                        elif aid == 242 and attr.get("raw", {}).get("value") is not None:  # Total_LBAs_Read
                            datos["total_lecturas"] = f"{attr['raw']['value'] * 512 / 1e9:.1f} GB"
            except Exception as e:
                escribir_log_depuracion(f"Error interpretando JSON de smartctl: {e!r}")
    # SMART autorizado pero el sistema no lo expone: no presentar los valores por defecto como si fueran mediciones
    if SMART_AUTORIZADO and not ES_WINDOWS and (datos["fuente"].startswith("sysfs (sin") or datos["fuente"].startswith("Información")):
        datos["sin_smart"] = True
        datos["motivo_sin_smart"] = "no_disponible"
        datos["fuente"] = "SMART no disponible en este equipo (ejecuta con sudo o instala smartmontools)"
    elif not SMART_AUTORIZADO:
        datos["motivo_sin_smart"] = "no_autorizado"
    return datos

def obtener_capacidad_unidad(letra_unidad):
    """Capacidad total/usada/libre de una unidad lógica en GB (base 1024, como el Explorador)."""
    try:
        total, usado, libre = shutil.disk_usage(ruta_raiz(letra_unidad))
        gb = 1024 ** 3
        return {"total_gb": round(total / gb, 2), "usado_gb": round(usado / gb, 2), "libre_gb": round(libre / gb, 2), "usado_bytes": usado}
    except Exception as e:
        escribir_log_depuracion(f"No se pudo leer capacidad de {letra_unidad}: {e!r}")
        return {"total_gb": 0.0, "usado_gb": 0.0, "libre_gb": 0.0, "usado_bytes": 0}

# ==========================================
# AUDITORÍA: INVENTARIO, HALLAZGOS Y CATEGORÍAS
# ==========================================
def obtener_info_auditoria(disk_num, letra_unidad):
    """Inventario del activo: serie, firmware, bus, salud, partición, sistema de archivos y BitLocker."""
    if not ES_WINDOWS:
        return _inventario_posix(disk_num, letra_unidad)
    letra = letra_unidad.rstrip(":")[:1]
    ps = r"""
    $ErrorActionPreference = 'SilentlyContinue'
    $pd = Get-PhysicalDisk | Where-Object { $_.DeviceId -eq __N__ }
    $dk = Get-Disk -Number __N__
    $vol = Get-Volume -DriveLetter '__L__'
    $bl = $null
    try { $bl = (New-Object -ComObject Shell.Application).NameSpace('__L__:').Self.ExtendedProperty('System.Volume.BitLockerProtection') } catch {}
    $serial = if ($pd.SerialNumber) { ([string]$pd.SerialNumber).Trim() } else { $null }
    [pscustomobject]@{
        serial = $serial; firmware = [string]$pd.FirmwareVersion; bus = [string]$pd.BusType
        media = [string]$pd.MediaType; salud = [string]$pd.HealthStatus; operacional = [string]$pd.OperationalStatus
        particion = [string]$dk.PartitionStyle; fs = [string]$vol.FileSystem; etiqueta = [string]$vol.FileSystemLabel
        salud_volumen = [string]$vol.HealthStatus; bitlocker = $bl
        disco_gb = if ($pd.Size) { [math]::Round($pd.Size / 1GB, 2) } else { $null }
    } | ConvertTo-Json -Compress
    """.replace("__N__", str(int(disk_num))).replace("__L__", letra)
    base = {"serial": "N/D", "firmware": "N/D", "bus": "N/D", "media": "N/D", "salud": "N/D", "operacional": "N/D",
            "particion": "N/D", "fs": "N/D", "etiqueta": "", "salud_volumen": "N/D", "bitlocker": "N/D", "disco_gb": None}
    try:
        crudo = ejecutar_comando_powershell(ps)
        j = json.loads(crudo) if crudo else {}
        for k in base:
            v = j.get(k)
            if v not in (None, ""):
                base[k] = v
        bl = j.get("bitlocker")
        base["bitlocker"] = {1: "Activado", 2: "Desactivado"}.get(bl, "N/D" if bl is None else f"Estado {bl}")
    except Exception as e:
        escribir_log_depuracion(f"Inventario de {letra_unidad}: {e!r}")
    return base

# Metadatos de parámetros adicionales: clave -> (etiqueta, criterio, ¿es anómalo?, severidad)
EXTRA_META = {
    "critical_warning": ("Advertencia crítica NVMe (bitmask)", "= 0", lambda v: v != 0, "Alta"),
    "available_spare": ("Reserva disponible (%)", "> umbral de reserva", None, "Alta"),
    "available_spare_threshold": ("Umbral de reserva (%)", "Informativo", None, "Baja"),
    "power_cycles": ("Ciclos de encendido", "Informativo", None, "Baja"),
    "unsafe_shutdowns": ("Apagados inesperados", "Informativo", None, "Baja"),
    "media_errors": ("Errores de integridad de medio", "= 0", lambda v: v > 0, "Alta"),
    "reallocated_sectors": ("Sectores reasignados (ID 5)", "= 0", lambda v: v > 0, "Media"),
    "pending_sectors": ("Sectores pendientes (ID 197)", "= 0", lambda v: v > 0, "Alta"),
    "uncorrectable_sectors": ("Sectores incorregibles (ID 198)", "= 0", lambda v: v > 0, "Alta"),
    "crc_errors": ("Errores CRC de interfaz (ID 199)", "= 0", lambda v: v > 0, "Baja"),
    "power_cycle_count": ("Ciclos de encendido (ID 12)", "Informativo", None, "Baja"),
    "smart_status": ("Estado SMART (macOS)", "= Verified", lambda v: str(v) not in ("Verified", "Not Supported"), "Alta"),
}

def evaluar_hallazgos(m):
    """Genera hallazgos y recomendaciones por unidad y un nivel de conformidad."""
    h = []
    add = lambda sev, texto, rec: h.append({"severidad": sev, "hallazgo": texto, "recomendacion": rec})
    extra = m.get("extra", {})
    inv = m.get("inventario", {})

    if m.get("sin_smart"):
        if m.get("motivo_sin_smart") == "no_disponible":
            add("Baja", "Este equipo no expuso datos SMART: temperatura, horas de uso, desgaste y errores no fueron medidos.",
                "Ejecutar con privilegios de administrador/root, instalar smartmontools o usar la herramienta del fabricante.")
        else:
            add("Baja", "El usuario no autorizó la lectura SMART: temperatura, horas de uso, desgaste y errores no fueron medidos.",
                "Repetir la prueba autorizando SMART si se requiere evaluar el estado de salud del disco.")
    if m.get("sin_smart"):
        pass
    elif m["temp_final"] >= UMBRAL_TEMP_CRITICA:
        add("Alta", f"Temperatura final de {m['temp_final']} °C (umbral {UMBRAL_TEMP_CRITICA} °C).",
            "Revisar ventilación, disipador y flujo de aire; repetir la medición tras corregir.")
    if m.get("sin_smart"):
        pass
    elif m["desgaste"] >= UMBRAL_DESGASTE_CRITICO:
        add("Alta", f"Vida útil consumida: {m['desgaste']}% (umbral crítico {UMBRAL_DESGASTE_CRITICO}%).",
            "Planificar reemplazo y respaldar la información de inmediato.")
    elif m["desgaste"] >= UMBRAL_DESGASTE_ADVERTENCIA:
        add("Media", f"Vida útil consumida: {m['desgaste']}% (advertencia desde {UMBRAL_DESGASTE_ADVERTENCIA}%).",
            "Programar el reemplazo y verificar los respaldos.")
    for clave, (etq, criterio, anomalo, sev) in EXTRA_META.items():
        v = extra.get(clave)
        try:
            es_anomalo = v is not None and anomalo is not None and bool(anomalo(v))
        except TypeError:
            es_anomalo = False
        if es_anomalo:
            add(sev, f"{etq} = {v} (criterio: {criterio}).",
                "Respaldar los datos, monitorear la tendencia y evaluar el reemplazo del disco.")
    sp, umbral_sp = extra.get("available_spare"), extra.get("available_spare_threshold")
    if isinstance(sp, (int, float)) and isinstance(umbral_sp, (int, float)) and sp <= umbral_sp:
        add("Alta", f"Reserva disponible ({sp}%) en o por debajo del umbral ({umbral_sp}%).", "Reemplazar el disco.")
    salud = str(inv.get("salud", "N/D"))
    if salud not in ("Healthy", "N/D"):
        add("Alta", f"Estado de salud reportado por Windows: {salud}.", "Respaldar y revisar el disco con el fabricante.")
    if m.get("total_gb") and m["usado_gb"] / m["total_gb"] * 100 >= UMBRAL_USO_CRITICO:
        add("Media", f"Capacidad utilizada {m['usado_gb'] / m['total_gb'] * 100:.1f}% (umbral {UMBRAL_USO_CRITICO}%).",
            "Liberar espacio o ampliar la capacidad.")
    if inv.get("bitlocker") == "Desactivado":
        add("Media", "Cifrado BitLocker desactivado en el volumen.",
            "Evaluar activar el cifrado según la política de seguridad de la información (ISO/IEC 27001, control A.8.24).")
    if not m.get("sin_smart") and ("respaldo" in str(m.get("fuente", "")).lower() or "sin smart" in str(m.get("fuente", "")).lower()):
        add("Baja", "Datos SMART limitados (se usó el contador de confiabilidad de Windows).",
            "Complementar con smartctl o con la herramienta del fabricante si se requiere mayor detalle.")
    sev = {x["severidad"] for x in h}
    conf = "NO CONFORME" if "Alta" in sev else ("OBSERVACIÓN" if h else "CONFORME")
    return h, conf

def parametros_smart(m):
    """Tabla de parámetros interpretados: (parámetro, valor, estado, criterio)."""
    extra = m.get("extra", {})
    if m.get("sin_smart"):
        return [(n, "N/D", "INFO", "SMART no disponible" if m.get("motivo_sin_smart") == "no_disponible" else "SMART no autorizado") for n in
                ("Temperatura actual (°C)", "Horas de encendido", "Desgaste / vida útil consumida (%)", "Errores de lectura", "Errores de escritura")]
    filas = [
        ("Temperatura actual (°C)", m["temp_final"], "ALERTA" if m["temp_final"] >= UMBRAL_TEMP_CRITICA else "OK", f"< {UMBRAL_TEMP_CRITICA} °C"),
        ("Temperatura inicial (°C)", m["temp_inicial"], "INFO", "Informativo"),
        ("Horas de encendido", m["horas_encendido"], "INFO", "Informativo"),
        ("Desgaste / vida útil consumida (%)", m["desgaste"], "ALERTA" if m["desgaste"] >= UMBRAL_DESGASTE_CRITICO else "OK", f"< {UMBRAL_DESGASTE_CRITICO}%"),
        ("Errores de lectura", m["err_lectura"], "ALERTA" if m["err_lectura"] > 0 else "OK", "= 0"),
        ("Errores de escritura", m["err_escritura"], "ALERTA" if m["err_escritura"] > 0 else "OK", "= 0"),
        ("Total leído", m["total_lecturas"], "INFO", "Informativo"),
        ("Total escrito", m["total_escrituras"], "INFO", "Informativo"),
    ]
    for clave, v in extra.items():
        etq, criterio, anomalo, _ = EXTRA_META.get(clave, (clave, "Informativo", None, "Baja"))
        try:
            estado = "INFO" if not anomalo else ("ALERTA" if anomalo(v) else "OK")
        except TypeError:
            estado = "INFO"
        if clave == "available_spare" and isinstance(v, (int, float)) and isinstance(extra.get("available_spare_threshold"), (int, float)):
            estado = "ALERTA" if v <= extra["available_spare_threshold"] else "OK"
        filas.append((etq, v, estado, criterio))
    return filas

def obtener_metadatos_auditoria(modo_texto, analizar_categorias):
    """Datos de la corrida para trazabilidad (equipo, usuario, SO, versión, ID de ejecución)."""
    try:
        usuario = getpass.getuser()
    except Exception:
        usuario = "N/D"
    return {
        "id": str(uuid.uuid4()), "equipo": platform.node(), "usuario": usuario,
        "so": f"{platform.system()} {platform.release()} ({platform.version()})", "arquitectura": platform.machine(),
        "python": platform.python_version(), "version": VERSION_SCRIPT, "modo": modo_texto,
        "categorias_analizadas": "Sí" if analizar_categorias else "No",
        "inicio": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "fin": "", "duracion": "",
        "_t0": time.time(),
    }

# ---- Categorías de archivos ----
CATEGORIAS = ["Sistema Operativo", "Programas y Aplicaciones", "Imágenes y Videos", "Audio", "Documentos",
              "Comprimidos y Respaldos", "Desarrollo y Bases de Datos", "Temporales y Archivos de Sistema", "Otros"]
CAT_NO_CLASIFICADO = "No clasificado / Sin acceso"
COLORES_CATEGORIAS = ["#64748b", "#3b82f6", "#8b5cf6", "#ec4899", "#10b981", "#f59e0b", "#06b6d4", "#f97316", "#94a3b8", "#334155"]

_EXT = {
    "Imágenes y Videos": "jpg jpeg png gif bmp tif tiff webp heic heif raw cr2 nef arw dng svg psd ico mp4 mkv avi mov wmv flv webm m4v mpg mpeg 3gp",
    "Audio": "mp3 wav flac aac ogg wma m4a aiff opus",
    "Documentos": "doc docx xls xlsx xlsm ppt pptx pdf txt rtf odt ods odp csv md epub msg pst ost",
    "Comprimidos y Respaldos": "zip rar 7z tar gz tgz bz2 xz iso img bak vhd vhdx vmdk vdi wim cab ova",
    "Desarrollo y Bases de Datos": "py js ts jsx tsx java c cpp h cs go rs php rb sh ps1 bat json xml yml yaml html css sql db sqlite sqlite3 mdb accdb mdf ldf ipynb pyc",
    "Programas y Aplicaciones": "exe msi dll apk appx msix",
    "Temporales y Archivos de Sistema": "tmp temp log etl dmp cache crdownload part",
}
MAPA_EXT = {e: cat for cat, exts in _EXT.items() for e in exts.split()}
ARCHIVOS_SISTEMA = {"pagefile.sys", "hiberfil.sys", "swapfile.sys"}
# Reglas por carpeta de primer nivel de la unidad
REGLAS_RAIZ = {"windows": "Sistema Operativo", "program files": "Programas y Aplicaciones",
               "program files (x86)": "Programas y Aplicaciones", "programdata": "Programas y Aplicaciones",
               "$recycle.bin": "Temporales y Archivos de Sistema", "system volume information": "Temporales y Archivos de Sistema",
               "$windows.~bt": "Sistema Operativo", "windows.old": "Sistema Operativo"}
REGLAS_CUALQUIER_NIVEL = {"temp": "Temporales y Archivos de Sistema", "tmp": "Temporales y Archivos de Sistema",
                          "node_modules": "Desarrollo y Bases de Datos", ".git": "Desarrollo y Bases de Datos"}
if not ES_WINDOWS:
    REGLAS_RAIZ.update({"usr": "Sistema Operativo", "bin": "Sistema Operativo", "sbin": "Sistema Operativo",
                        "lib": "Sistema Operativo", "lib64": "Sistema Operativo", "etc": "Sistema Operativo",
                        "boot": "Sistema Operativo", "system": "Sistema Operativo", "library": "Sistema Operativo",
                        "private": "Sistema Operativo", "opt": "Programas y Aplicaciones", "applications": "Programas y Aplicaciones",
                        "snap": "Programas y Aplicaciones", "var": "Temporales y Archivos de Sistema", "tmp": "Temporales y Archivos de Sistema"})
SALTAR_RAIZ_POSIX = {"proc", "sys", "dev", "run"}
_ATTR_REPARSE, _ATTR_NUBE = 0x400, 0x1000 | 0x40000 | 0x400000   # reparse point / archivo solo en la nube

def clasificar_archivo(nombre):
    n = nombre.lower()
    if n in ARCHIVOS_SISTEMA:
        return "Temporales y Archivos de Sistema"
    return MAPA_EXT.get(n.rsplit(".", 1)[-1] if "." in n else "", "Otros")

def analizar_categorias(raiz, usado_bytes):
    """Recorre la unidad sumando el tamaño lógico por categoría. Ctrl+C detiene el análisis y conserva lo contado."""
    cats = {c: {"bytes": 0, "archivos": 0} for c in CATEGORIAS}
    total_archivos, inaccesibles, completo = 0, 0, True
    t0 = time.time()
    pila = [(raiz, None, True)]
    try:
        dev_raiz = os.stat(raiz).st_dev if not ES_WINDOWS else None
    except OSError:
        dev_raiz = None
    try:
        while pila:
            _verificar_cancelacion()
            ruta, cat_dir, es_raiz = pila.pop()
            try:
                with os.scandir(ruta) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                attrs = getattr(e.stat(follow_symlinks=False), "st_file_attributes", 0)
                                if attrs & _ATTR_REPARSE:
                                    continue  # junctions/enlaces: evita ciclos y doble conteo
                                nom = e.name.lower()
                                if not ES_WINDOWS:
                                    if es_raiz and nom in SALTAR_RAIZ_POSIX:
                                        continue
                                    if SISTEMA == "Linux" and dev_raiz is not None and e.stat(follow_symlinks=False).st_dev != dev_raiz:
                                        continue  # otro sistema de archivos montado: se cuenta como otra unidad
                                nueva = cat_dir or (REGLAS_RAIZ.get(nom) if es_raiz else None) or REGLAS_CUALQUIER_NIVEL.get(nom)
                                pila.append((e.path, nueva, False))
                            elif e.is_file(follow_symlinks=False):
                                st = e.stat(follow_symlinks=False)
                                if getattr(st, "st_file_attributes", 0) & _ATTR_NUBE:
                                    continue  # marcador de OneDrive: no ocupa espacio local
                                cat = cat_dir or clasificar_archivo(e.name)
                                cats[cat]["bytes"] += st.st_size
                                cats[cat]["archivos"] += 1
                                total_archivos += 1
                                if total_archivos % 50000 == 0:
                                    sys.stdout.write(f"\r       Analizando... {total_archivos:,} archivos ({int(time.time() - t0)}s)")
                                    sys.stdout.flush()
                        except OSError:
                            inaccesibles += 1
            except OSError:
                inaccesibles += 1
    except KeyboardInterrupt:
        completo = False
        print("\n       [AVISO] Análisis interrumpido por el usuario; se reportan resultados parciales.")
    escaneado = sum(c["bytes"] for c in cats.values())
    sys.stdout.write(f"\r       Análisis finalizado: {total_archivos:,} archivos en {int(time.time() - t0)}s{' ' * 20}\n")
    return {"categorias": cats, "no_clasificado_bytes": max(0, usado_bytes - escaneado), "escaneado_bytes": escaneado,
            "archivos_total": total_archivos, "inaccesibles": inaccesibles,
            "segundos": round(time.time() - t0, 1), "completo": completo}

def mostrar_barra_progreso(duracion_segundos, archivo_obj, chunk_data):
    """Muestra una barra de progreso animada en la consola mientras escribe bloques en el archivo."""
    ancho_barra = 30
    t_inicio = time.perf_counter()
    
    while True:
        _verificar_cancelacion()
        transcurrido = time.perf_counter() - t_inicio
        if transcurrido >= duracion_segundos:
            break
            
        archivo_obj.write(chunk_data)
        
        porcentaje = min(1.0, transcurrido / duracion_segundos)
        completado = int(ancho_barra * porcentaje)
        barra = "█" * completado + "░" * (ancho_barra - completado)
        porc_str = f"{int(porcentaje * 100)}%"
        
        sys.stdout.write(f"\r       [{barra}] {porc_str} ({int(transcurrido)}s / {duracion_segundos}s)")
        sys.stdout.flush()
        time.sleep(0.05)
        
    sys.stdout.write(f"\r       [{'█' * ancho_barra}] 100% ({duracion_segundos}s / {duracion_segundos}s) - OK\n")
    sys.stdout.flush()

def ejecutar_benchmark(letra_unidad, disk_num, smartctl_bin, modo_avanzado):
    """Ejecuta el test según el modo seleccionado con barra de progreso en consola."""
    letra = letra_unidad.rstrip(":")
    info_inicial = extraer_smart_profundo(smartctl_bin, disk_num, letra_unidad)
    
    print(f"    - Modelo: {info_inicial['modelo']} [Tecnología: {info_inicial['tecnologia']} | Disco Físico: {disk_num}]")
    base_temp = int(info_inicial['temp']) if str(info_inicial['temp']).isdigit() else 38
    print(f"    - Desgaste: {info_inicial['wear']}% | Temp Base: {base_temp} °C | Horas Encendido: {info_inicial['power_hours']}")
    
    archivo_prueba = ruta_archivo_prueba(letra_unidad)
    
    seq_read, seq_write = 0.0, 0.0
    rnd_write_iops = 0
    latencia_prom_ms = 0.0
    
    if modo_avanzado:
        timeline_temps = [base_temp, base_temp, base_temp, base_temp]
        bloque_size = 16 * 1024 * 1024
        duracion_fases = 4
        segundos_por_fase = 25
        
        try:
            print(f"    -> [MODO AVANZADO] Iniciando estrés térmico sostenido por fases...")
            t_total_ini = time.perf_counter()
            
            with open(archivo_prueba, "wb") as f:
                chunk_data = os.urandom(bloque_size)
                for fase in range(duracion_fases):
                    print(f"       [Fase {fase+1}/4] Ejecutando ráfaga de escritura sostenida...")
                    PROGRESO["frac"] = 0.55 * fase / duracion_fases
                    mostrar_barra_progreso(segundos_por_fase, f, chunk_data)
                    f.flush()
                    os.fsync(f.fileno())
                    
                    info_fase = extraer_smart_profundo(smartctl_bin, disk_num, letra_unidad)
                    t_actual = int(info_fase['temp']) if str(info_fase['temp']).isdigit() else (base_temp + (fase * 2))
                    timeline_temps[fase] = t_actual
                    print(f"       ➔ Temperatura Registrada: {t_actual} °C\n")

            t_total_dur = time.perf_counter() - t_total_ini
            total_escrito_mb = (duracion_fases * segundos_por_fase * 16)
            seq_write = round(total_escrito_mb / t_total_dur, 2) if t_total_dur > 0 else 0.0

            t_ini_r = time.perf_counter()
            with open(archivo_prueba, "rb") as f:
                _sin_cache(f)
                while f.read(bloque_size):
                    pass
            t_fin_r = time.perf_counter() - t_ini_r
            total_leido_mb = os.path.getsize(archivo_prueba) / (1024 * 1024)
            seq_read = round(total_leido_mb / t_fin_r, 2) if t_fin_r > 0 else 0.0

            if info_inicial['tecnologia'] != "HDD":
                bloque_4k = os.urandom(4096)
                iteraciones_4k = 15000
                t_ini_4kw = time.perf_counter()
                with open(archivo_prueba, "r+b") as f:
                    file_size = f.seek(0, os.SEEK_END)
                    for _ in range(iteraciones_4k):
                        pos = random.randint(0, max(0, file_size - 4096))
                        f.seek(pos)
                        f.write(bloque_4k)
                    f.flush()
                    os.fsync(f.fileno())
                t_fin_4kw = time.perf_counter() - t_ini_4kw
                iops_w = iteraciones_4k / t_fin_4kw if t_fin_4kw > 0 else 0.0
                latencia_prom_ms = round((t_fin_4kw / iteraciones_4k) * 1000, 4)
                rnd_write_iops = int(iops_w)
            else:
                rnd_write_iops = "N/A"
                latencia_prom_ms = "N/A"

        except Exception as e:
            print(f"    [AVISO] Error en test avanzado: {e}")
        finally:
            if os.path.exists(archivo_prueba):
                try: os.remove(archivo_prueba)
                except: pass
                
        info_final = extraer_smart_profundo(smartctl_bin, disk_num, letra_unidad)
        temp_final = int(info_final['temp']) if str(info_final['temp']).isdigit() else timeline_temps[-1]
        fases_labels = ["Fase 1 (Inicio)", "Fase 2 (Medio)", "Fase 3 (Avanzado)", "Fase 4 (Fin)"]

    else:
        tamanio_seq_mb = TAM_SEQ_HDD_MB if info_inicial['tecnologia'] == "HDD" else TAM_SEQ_SSD_MB
        bloque_size = 16 * 1024 * 1024
        
        try:
            print(f"    -> [MODO GENERAL] Ejecutando pruebas rápidas ({tamanio_seq_mb} MB)...")
            datos_seq = os.urandom(bloque_size)
            bloques_total = tamanio_seq_mb // 16
            
            t_ini_w = time.perf_counter()
            with open(archivo_prueba, "wb") as f:
                for _ in range(bloques_total):
                    f.write(datos_seq)
                f.flush()
                os.fsync(f.fileno())
            t_fin_w = time.perf_counter() - t_ini_w
            seq_write = round(tamanio_seq_mb / t_fin_w, 2) if t_fin_w > 0 else 0.0
            PROGRESO["frac"] = 0.2
            
            t_ini_r = time.perf_counter()
            with open(archivo_prueba, "rb") as f:
                _sin_cache(f)
                while f.read(bloque_size):
                    pass
            t_fin_r = time.perf_counter() - t_ini_r
            seq_read = round(tamanio_seq_mb / t_fin_r, 2) if t_fin_r > 0 else 0.0
            PROGRESO["frac"] = 0.35

            if info_inicial['tecnologia'] != "HDD":
                bloque_4k = os.urandom(4096)
                iteraciones_4k = 4000
                t_ini_4kw = time.perf_counter()
                with open(archivo_prueba, "r+b") as f:
                    file_size = f.seek(0, os.SEEK_END)
                    for _ in range(iteraciones_4k):
                        pos = random.randint(0, max(0, file_size - 4096))
                        f.seek(pos)
                        f.write(bloque_4k)
                    f.flush()
                    os.fsync(f.fileno())
                t_fin_4kw = time.perf_counter() - t_ini_4kw
                iops_w = iteraciones_4k / t_fin_4kw if t_fin_4kw > 0 else 0.0
                latencia_prom_ms = round((t_fin_4kw / iteraciones_4k) * 1000, 4)
                rnd_write_iops = int(iops_w)
            else:
                rnd_write_iops = "N/A"
                latencia_prom_ms = "N/A"

        except Exception as e:
            print(f"    [AVISO] Error en test general: {e}")
        finally:
            if os.path.exists(archivo_prueba):
                try: os.remove(archivo_prueba)
                except: pass

        info_final = extraer_smart_profundo(smartctl_bin, disk_num, letra_unidad)
        temp_final = int(info_final['temp']) if str(info_final['temp']).isdigit() else base_temp
        timeline_temps = [base_temp, temp_final]
        fases_labels = ["Inicio (Reposo)", "Fin (Post-Test)"]

    print(f"    [OK] L={seq_read} MB/s | E={seq_write} MB/s | IOPS 4K: {rnd_write_iops} | Latencia: {latencia_prom_ms} ms")

    alertas_detectadas = []
    try:
        if temp_final >= UMBRAL_TEMP_CRITICA:
            alertas_detectadas.append("ALERTA_TEMPERATURA_ALTA")
    except ValueError:
        pass

    try:
        if int(info_inicial['wear']) >= UMBRAL_DESGASTE_CRITICO:
            alertas_detectadas.append("ALERTA_DESGASTE_CRITICO")
    except ValueError:
        pass

    estado_alerta = "ESTABLE" if not alertas_detectadas else " | ".join(alertas_detectadas)
    if info_inicial.get("sin_smart"):
        estado_alerta = "SIN_SMART"

    return {
        "modelo": info_inicial['modelo'], "tecnologia": info_inicial['tecnologia'], 
        "desgaste": int(info_inicial['wear']) if info_inicial['wear'].isdigit() else 0,
        "temp_inicial": base_temp, "temp_final": temp_final,
        "timeline_temps": timeline_temps, "fases_labels": fases_labels,
        "horas_encendido": info_inicial['power_hours'], 
        "fecha_primer_uso": info_inicial['fecha_inicio'],
        "total_lecturas": info_inicial['total_lecturas'], "total_escrituras": info_inicial['total_escrituras'],
        "err_lectura": int(info_inicial['r_err']) if info_inicial['r_err'].isdigit() else 0, 
        "err_escritura": int(info_inicial['w_err']) if info_inicial['w_err'].isdigit() else 0,
        "lectura_seq": seq_read, "escritura_seq": seq_write,
        "iops_aleatorio": rnd_write_iops, "latencia_ms": latencia_prom_ms,
        "estado_alerta": estado_alerta,
        "smart_json_raw": info_inicial['smart_json_raw'],
        "extra": info_inicial.get("extra", {}), "fuente": info_inicial.get("fuente", "N/D"),
        "sin_smart": bool(info_inicial.get("sin_smart")), "motivo_sin_smart": info_inicial.get("motivo_sin_smart", "")
    }

EXCEL_JS_TEMPLATE = r'''
const DATOS_EXCEL = __DATA__;

function exportarExcel() {
    const D = DATOS_EXCEL, A = D.auditoria || {}, U = A.umbrales || {};
    const borde = { style: "thin", color: { rgb: "D9D9D9" } };
    const bordes = { top: borde, bottom: borde, left: borde, right: borde };
    const centro = { horizontal: "center", vertical: "center", wrapText: true };
    const izq = { horizontal: "left", vertical: "center", wrapText: true };

    const T = (v) => ({ v, t: "s", s: { font: { name: "Calibri", sz: 16, bold: true, color: { rgb: "1F4E78" } } } });
    const S = (v) => ({ v, t: "s", s: { font: { name: "Calibri", sz: 12, bold: true, color: { rgb: "1F4E78" } },
        border: { bottom: { style: "medium", color: { rgb: "1F4E78" } } } } });
    const H = (v) => ({ v, t: "s", s: { font: { name: "Calibri", sz: 11, bold: true, color: { rgb: "FFFFFF" } },
        fill: { fgColor: { rgb: "1F4E78" } }, alignment: centro, border: bordes } });
    const C = (v, extra = {}) => {
        if (v === null || v === undefined || v === "") v = "N/A";
        const num = typeof v === "number" && isFinite(v);
        const celda = { v, t: num ? "n" : "s", s: {
            font: Object.assign({ name: "Calibri", sz: 10 }, extra.font || {}),
            alignment: extra.align || centro, border: bordes } };
        if (extra.fill) celda.s.fill = { fgColor: { rgb: extra.fill } };
        if (extra.z && num) celda.z = extra.z;
        return celda;
    };
    const L = (v) => C(v, { font: { bold: true }, fill: "EEF3F8", align: izq });
    const V = (v, extra = {}) => C(v, Object.assign({ align: izq }, extra));

    const VERDE = { fill: "D1FAE5", font: { bold: true, color: { rgb: "065F46" } } };
    const AMARILLO = { fill: "FEF3C7", font: { bold: true, color: { rgb: "92400E" } } };
    const ROJO = { fill: "FEE2E2", font: { bold: true, color: { rgb: "991B1B" } } };
    const AZUL = { fill: "DBEAFE", font: { bold: true, color: { rgb: "1E40AF" } } };
    const estiloConf = (c) => c === "CONFORME" ? VERDE : (c === "OBSERVACIÓN" ? AMARILLO : ROJO);
    const estiloSev = (s) => s === "Alta" ? ROJO : (s === "Media" ? AMARILLO : AZUL);
    const estiloEstado = (e) => e === "OK" ? VERDE : (e === "ALERTA" ? ROJO : {});

    const auto = (filas, min = 10, max = 60) => {
        const n = Math.max(...filas.map(f => f.length));
        const anchos = [];
        for (let c = 0; c < n; c++) {
            let m = min;
            filas.forEach(f => { const x = f[c]; if (x && x.v !== undefined) m = Math.max(m, String(x.v).length + 3); });
            anchos.push({ wch: Math.min(m, max) });
        }
        return anchos;
    };
    const hoja = (filas, extra = {}) => {
        const ws = XLSX.utils.aoa_to_sheet(filas);
        // Los anchos DEBEN ser objetos {wch}; un número suelto genera XML inválido y Excel descarta la hoja.
        ws['!cols'] = (extra.anchos || auto(filas)).map(a => typeof a === "number" ? { wch: a } : a);
        if (extra.filtro) ws['!autofilter'] = { ref: extra.filtro };
        if (extra.merges) ws['!merges'] = extra.merges;
        if (extra.filas) ws['!rows'] = extra.filas;
        return ws;
    };
    const agregar = (wb, nombre, ws) => { wb.SheetNames.push(nombre); wb.Sheets[nombre] = ws; };
    const rangoFiltro = (filas, cols) => "A1:" + XLSX.utils.encode_cell({ r: filas.length - 1, c: cols - 1 });
    const GB = '#,##0.00" GB"', PCT = '0.0%';

    // Respeta los filtros activos de la tabla del dashboard
    const visibles = new Set();
    document.querySelectorAll('#dataTable tbody tr').forEach(tr => {
        if (tr.style.display !== 'none') visibles.add(tr.cells[0].innerText.trim());
    });
    const unidades = D.unidades.filter(u => visibles.has(u.unidad));
    const idx = D.unidades.map((u, i) => visibles.has(u.unidad) ? i : -1).filter(i => i >= 0);
    const wb = XLSX.utils.book_new();

    // ======================= 1. RESUMEN =======================
    const k = D.kpis;
    const res = [], merges = [], altos = [];
    const fila = (...celdas) => { res.push(celdas); return res.length - 1; };
    fila(T("Reporte de Diagnóstico de Almacenamiento"));
    fila(V("Evidencia técnica de apoyo para auditoría (ISO/IEC 27001: A.5.9 · A.7.13 · A.8.24)", { font: { italic: true, color: { rgb: "64748B" } } }));
    fila();
    fila(S("Datos de la auditoría"), S(""));
    [["ID de ejecución", A.id], ["Equipo", A.equipo], ["Usuario", A.usuario], ["Sistema operativo", A.so],
     ["Arquitectura", A.arquitectura], ["Versión de Python", A.python], ["Versión del script", A.version],
     ["Modo de prueba", A.modo], ["Análisis por categorías", A.categorias_analizadas],
     ["Inicio", A.inicio], ["Fin", A.fin], ["Duración", A.duracion],
     ["Huella SHA-256 de los datos", A.huella]].forEach(([a, b]) => fila(L(a), V(b)));
    fila();
    fila(S("Criterios y umbrales aplicados"), S(""));
    [["Temperatura crítica", U.temp + " °C"], ["Desgaste (advertencia)", U.desgaste_adv + " %"],
     ["Desgaste (crítico)", U.desgaste_crit + " %"], ["Uso de capacidad (crítico)", U.uso_crit + " %"]]
        .forEach(([a, b]) => fila(L(a), V(b)));
    fila();
    fila(S("Indicadores generales"), S(""));
    fila(L("Total de unidades"), V(k.total_unidades));
    fila(L("Unidades NVMe"), V(k.nvme));
    fila(L("Temperatura máxima"), V(k.temp_max, { z: '0" °C"' }));
    fila(L("Alertas activas"), V(k.alertas, k.alertas > 0 ? ROJO : VERDE));
    fila(L("Hallazgos registrados"), V(D.hallazgos.length, D.hallazgos.length > 0 ? AMARILLO : VERDE));
    fila();
    fila(S("Capacidad general"), S(""));
    fila(L("Almacenamiento total"), V(k.cap_total, { z: GB }));
    fila(L("Total usado"), V(k.cap_usado, { z: GB }));
    fila(L("Total disponible"), V(k.cap_libre, { z: GB }));
    fila(L("Uso global"), V(k.cap_total ? k.cap_usado / k.cap_total : 0, { z: PCT }));
    fila();
    fila(S("Conformidad por unidad"), S(""), S(""), S(""));
    fila(H("Unidad"), H("Modelo"), H("Conformidad"), H("N.º de hallazgos"));
    D.unidades.forEach(u => fila(C(u.unidad, { font: { bold: true } }), C(u.modelo), C(u.conformidad, estiloConf(u.conformidad)), C(u.n_hallazgos)));
    fila();
    fila(S("Destacados"), S(""), S(""));
    fila(H("Indicador"), H("Unidad"), H("Valor"));
    D.destacados.forEach(d => fila(L(d.indicador), C(d.unidad), C(d.valor)));
    fila();
    fila(S("Hallazgos y recomendaciones"), S(""), S(""), S(""));
    fila(H("Unidad"), H("Severidad"), H("Hallazgo"), H("Recomendación"));
    if (D.hallazgos.length === 0) fila(C("—"), C("—"), C("Sin hallazgos: todas las unidades cumplen los criterios aplicados.", { align: izq }), C("—"));
    D.hallazgos.forEach(h => fila(C(h.unidad, { font: { bold: true } }), C(h.severidad, estiloSev(h.severidad)),
        C(h.hallazgo, { align: izq }), C(h.recomendacion, { align: izq })));
    fila();
    fila(S("Notas y limitaciones"), S(""), S(""), S(""));
    D.notas.forEach(n => { const r = fila(V(n)); merges.push({ s: { r, c: 0 }, e: { r, c: 3 } }); altos[r] = { hpx: 48 }; });
    agregar(wb, "Resumen", hoja(res, { anchos: [34, 40, 58, 58], merges, filas: altos.map(a => a || {}) }));

    // ======================= 2. INVENTARIO DE ACTIVOS =======================
    const invCab = ["Unidad", "Modelo", "N.º de serie", "Firmware", "Tipo de bus", "Tipo de medio", "Tamaño del disco",
        "Estilo de partición", "Sistema de archivos", "Etiqueta de volumen", "Estado de salud (Windows)", "Estado operativo",
        "Salud del volumen", "Cifrado BitLocker", "Fuente de datos SMART"];
    const inv = [invCab.map(H)];
    unidades.forEach(u => { const i = u.inv || {};
        inv.push([C(u.unidad, { font: { bold: true } }), C(u.modelo), C(i.serial), C(i.firmware), C(i.bus), C(i.media),
            C(i.disco_gb, { z: GB }), C(i.particion), C(i.fs), C(i.etiqueta), C(i.salud, i.salud === "Healthy" ? VERDE : {}),
            C(i.operacional), C(i.salud_volumen), C(i.bitlocker, i.bitlocker === "Activado" ? VERDE : (i.bitlocker === "Desactivado" ? AMARILLO : {})),
            C(u.fuente)]);
    });
    agregar(wb, "Inventario de Activos", hoja(inv, { filtro: rangoFiltro(inv, invCab.length) }));

    // ======================= 3. DETALLE TÉCNICO =======================
    const cab = ["Unidad", "Modelo", "Tecnología", "Desgaste", "Horas de Uso", "Primer Uso (aprox.)",
        "Temp. Inicial", "Temp. Final", "Δ Temp.", "Lectura Sec.", "Escritura Sec.", "IOPS 4K", "Latencia (ms)",
        "Total Leído", "Total Escrito", "Errores Lectura", "Errores Escritura",
        "Capacidad Total", "Usado", "Disponible", "% Uso", "Estado", "Conformidad"];
    const det = [cab.map(H)];
    unidades.forEach(u => {
        det.push([
            C(u.unidad, { font: { bold: true } }), C(u.modelo), C(u.tecnologia),
            C(u.desgaste, { z: '0"%"' }), C(u.horas, { z: '#,##0" h"' }), C(u.primer_uso),
            C(u.temp_i, { z: '0" °C"' }), C(u.temp_f, { z: '0" °C"' }), C((typeof u.temp_f === "number" && typeof u.temp_i === "number") ? u.temp_f - u.temp_i : "N/D", { z: '+0" °C";-0" °C";0" °C"' }),
            C(u.lectura, { z: '#,##0.00" MB/s"' }), C(u.escritura, { z: '#,##0.00" MB/s"' }),
            C(u.iops, { z: '#,##0' }), C(u.latencia, { z: '0.0000' }),
            C(u.total_lecturas), C(u.total_escrituras),
            C(u.err_l, { z: '#,##0' }), C(u.err_e, { z: '#,##0' }),
            C(u.cap_total, { z: GB }), C(u.usado, { z: GB }), C(u.libre, { z: GB }),
            C(u.cap_total ? u.usado / u.cap_total : 0, { z: PCT }),
            C(u.estado, u.estado === "ESTABLE" ? VERDE : (u.estado === "SIN_SMART" ? AMARILLO : ROJO)), C(u.conformidad, estiloConf(u.conformidad))
        ]);
    });
    agregar(wb, "Detalle Técnico", hoja(det, { filtro: rangoFiltro(det, cab.length) }));

    // ======================= 4. CAPACIDAD =======================
    const capCab = ["Unidad", "Modelo", "Capacidad Total", "Usado", "Disponible", "% Uso", "% Disponible"];
    const cap = [capCab.map(H)];
    let sT = 0, sU = 0, sL = 0;
    unidades.forEach(u => {
        sT += u.cap_total; sU += u.usado; sL += u.libre;
        cap.push([C(u.unidad, { font: { bold: true } }), C(u.modelo), C(u.cap_total, { z: GB }), C(u.usado, { z: GB }), C(u.libre, { z: GB }),
            C(u.cap_total ? u.usado / u.cap_total : 0, { z: PCT }), C(u.cap_total ? u.libre / u.cap_total : 0, { z: PCT })]);
    });
    const tot = { fill: "DBEAFE", font: { bold: true } };
    cap.push([C("TOTAL GENERAL", tot), C("Todas las unidades", tot), C(sT, Object.assign({ z: GB }, tot)),
        C(sU, Object.assign({ z: GB }, tot)), C(sL, Object.assign({ z: GB }, tot)),
        C(sT ? sU / sT : 0, Object.assign({ z: PCT }, tot)), C(sT ? sL / sT : 0, Object.assign({ z: PCT }, tot))]);
    agregar(wb, "Capacidad", hoja(cap));

    // ======================= 5. CATEGORÍAS DE ARCHIVOS =======================
    if (D.categorias_general) {
        const catCab = ["Unidad", "Categoría", "Archivos", "Tamaño", "% del espacio usado"];
        const cat = [catCab.map(H)];
        unidades.forEach(u => { if (!u.cat) return;
            u.cat.filas.forEach(f => cat.push([C(u.unidad, { font: { bold: true } }), C(f.categoria), C(f.archivos, { z: '#,##0' }),
                C(f.gb, { z: GB }), C(f.pct, { z: PCT })]));
        });
        const filtroCat = rangoFiltro(cat, catCab.length);
        D.categorias_general.forEach(f => cat.push([C("GENERAL", tot), C(f.categoria, tot), C(f.archivos, Object.assign({ z: '#,##0' }, tot)),
            C(f.gb, Object.assign({ z: GB }, tot)), C(f.pct, Object.assign({ z: PCT }, tot))]));
        cat.push([]);
        cat.push([H("Unidad"), H("Archivos analizados"), H("Elementos inaccesibles"), H("Duración del análisis"), H("Resultado")]);
        unidades.forEach(u => { if (!u.cat) return;
            cat.push([C(u.unidad, { font: { bold: true } }), C(u.cat.archivos_total, { z: '#,##0' }), C(u.cat.inaccesibles, { z: '#,##0' }),
                C(u.cat.segundos, { z: '0.0" s"' }), C(u.cat.completo ? "Completo" : "Parcial (interrumpido)", u.cat.completo ? VERDE : AMARILLO)]);
        });
        agregar(wb, "Categorías de Archivos", hoja(cat, { anchos: [14, 38, 18, 20, 22], filtro: filtroCat }));
    }

    // ======================= 6. EVOLUCIÓN TÉRMICA =======================
    const term = [[H("Fase")].concat(idx.map(i => H(D.unidades[i].unidad + " - " + D.unidades[i].modelo)))];
    D.fases.forEach((fase, f) => term.push([L(fase)].concat(idx.map(i => C(D.timelines[i][f], { z: '0" °C"' })))));
    agregar(wb, "Evolución Térmica", hoja(term));

    // ======================= 7. REGISTRO SMART (parámetros interpretados) =======================
    const sm = [["Unidad", "Modelo", "Parámetro", "Valor", "Estado", "Criterio", "Fuente"].map(H)];
    unidades.forEach(u => u.parametros.forEach(p => sm.push([
        C(u.unidad, { font: { bold: true } }), C(u.modelo), C(p.parametro, { align: izq }),
        C(p.valor, { z: '#,##0' }), C(p.estado, estiloEstado(p.estado)), C(p.criterio), C(u.fuente)])));
    agregar(wb, "Registro SMART", hoja(sm, { filtro: rangoFiltro(sm, 7), anchos: [10, 30, 40, 18, 12, 22, 44] }));

    // ======================= 8. SMART JSON RAW =======================
    const raw = [["Unidad", "Modelo", "Fuente de datos", "Registro SMART (JSON)"].map(H)];
    unidades.forEach(u => {
        let texto = u.smart_json || "";
        try { texto = JSON.stringify(JSON.parse(texto), null, 2); } catch (e) {}
        if (texto.length > 32000) texto = texto.slice(0, 32000) + "\n... (truncado)";
        raw.push([C(u.unidad, { font: { bold: true } }), C(u.modelo), C(u.fuente),
            C(texto, { align: { horizontal: "left", vertical: "top", wrapText: false } })]);
    });
    agregar(wb, "SMART JSON Raw", hoja(raw, { anchos: [10, 32, 44, 100] }));

    const fechaStr = new Date().toISOString().slice(0, 10);
    XLSX.writeFile(wb, `Reporte_Almacenamiento_${fechaStr}.xlsx`);
}
'''

def generar_reporte_html(registros, archivo_salida, modo_texto, meta_auditoria=None):
    """Genera el HTML interactivo con sección analítica ejecutiva completa."""
    total_unidades = len(registros)
    nvme_count = sum(1 for r in registros if r["tecnologia"] == "NVMe")
    alertas_count = sum(1 for r in registros if "ALERTA" in r["estado_alerta"])
    con_smart = [r for r in registros if not r.get("sin_smart")]
    max_temp = max([r["temp_final"] for r in con_smart]) if con_smart else None
    max_temp_txt = f"{max_temp} °C" if max_temp is not None else "N/D"
    
    mas_rapido_lectura = max(registros, key=lambda x: x["lectura_seq"]) if registros else None
    mas_rapido_escritura = max(registros, key=lambda x: x["escritura_seq"]) if registros else None
    mas_estable = min(con_smart, key=lambda x: abs(x["temp_final"] - x["temp_inicial"])) if con_smart else None
    delta_txt = f"+{abs(mas_estable['temp_final'] - mas_estable['temp_inicial'])} °C" if mas_estable else "N/D"
    
    def obtener_horas(r):
        try: return int(r["horas_encendido"])
        except: return 0
    mas_longevo = max(con_smart, key=obtener_horas) if con_smart else None
    horas_long_txt = f"{mas_longevo['horas_encendido']}" if mas_longevo else "N/D"

    unidades_nombres = [f"{r['unidad']} - {r['modelo']}" for r in registros]
    seq_lectura = [r['lectura_seq'] for r in registros]
    seq_escritura = [r['escritura_seq'] for r in registros]
    
    # Capacidad por unidad y acumulada
    cap_usado = [r.get("usado_gb", 0) for r in registros]
    cap_libre = [r.get("libre_gb", 0) for r in registros]
    cap_total = [r.get("total_gb", 0) for r in registros]
    suma_usado, suma_libre, suma_total = round(sum(cap_usado), 2), round(sum(cap_libre), 2), round(sum(cap_total), 2)
    porc_uso_general = round(suma_usado / suma_total * 100, 1) if suma_total else 0

    fases_labels = registros[0]["fases_labels"] if registros else ["Inicio", "Fin"]
    datasets_termicos = []
    colores_chart = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6"]
    
    for idx, r in enumerate(registros):
        if r.get("sin_smart"):
            continue
        color = colores_chart[idx % len(colores_chart)]
        datasets_termicos.append({
            "label": f"{r['unidad']} ({r['modelo']})",
            "data": r["timeline_temps"],
            "borderColor": color,
            "backgroundColor": color,
            "tension": 0.3
        })

    # ---- Categorías de archivos ----
    GB = 1024 ** 3
    hay_cat = any(r.get("categorias_info") for r in registros)
    cat_nombres = CATEGORIAS + [CAT_NO_CLASIFICADO]
    cat_gb_unidad, cat_pct_unidad, cat_general_bytes = [], [], {c: 0 for c in cat_nombres}
    cat_general_archivos = {c: 0 for c in cat_nombres}
    filas_cat_unidad = []
    for r in registros:
        info = r.get("categorias_info")
        if not info:
            cat_gb_unidad.append([0] * len(cat_nombres)); cat_pct_unidad.append([0] * len(cat_nombres)); filas_cat_unidad.append(None)
            continue
        b = {c: info["categorias"][c]["bytes"] for c in CATEGORIAS}
        b[CAT_NO_CLASIFICADO] = info["no_clasificado_bytes"]
        base = sum(b.values()) or 1
        cat_gb_unidad.append([round(b[c] / GB, 2) for c in cat_nombres])
        cat_pct_unidad.append([round(b[c] / base * 100, 2) for c in cat_nombres])
        filas = []
        for c in cat_nombres:
            arch = info["categorias"][c]["archivos"] if c in info["categorias"] else 0
            cat_general_bytes[c] += b[c]; cat_general_archivos[c] += arch
            filas.append({"categoria": c, "archivos": arch, "gb": round(b[c] / GB, 2), "pct": round(b[c] / base, 4)})
        filas_cat_unidad.append({"filas": filas, "archivos_total": info["archivos_total"], "inaccesibles": info["inaccesibles"],
                                 "segundos": info["segundos"], "completo": info["completo"]})
    base_gen = sum(cat_general_bytes.values()) or 1
    cat_general_gb = [round(cat_general_bytes[c] / GB, 2) for c in cat_nombres]
    cat_general_excel = [{"categoria": c, "archivos": cat_general_archivos[c], "gb": round(cat_general_bytes[c] / GB, 2),
                          "pct": round(cat_general_bytes[c] / base_gen, 4)} for c in cat_nombres]

    # ---- Hallazgos y trazabilidad ----
    meta = dict(meta_auditoria or {})
    meta.pop("_t0", None)
    hallazgos_excel = [{"unidad": r["unidad"], **h} for r in registros for h in r.get("hallazgos", [])]
    huella_base = json.dumps([{k: v for k, v in r.items() if k != "smart_json_raw"} for r in registros],
                             sort_keys=True, ensure_ascii=False, default=str)
    meta["huella"] = hashlib.sha256(huella_base.encode("utf-8")).hexdigest()
    meta["umbrales"] = {"temp": UMBRAL_TEMP_CRITICA, "desgaste_adv": UMBRAL_DESGASTE_ADVERTENCIA,
                        "desgaste_crit": UMBRAL_DESGASTE_CRITICO, "uso_crit": UMBRAL_USO_CRITICO}
    notas_excel = [
        "Este reporte es evidencia técnica de apoyo (p. ej., inventario de activos y mantenimiento de equipos: ISO/IEC 27001:2022 controles A.5.9, A.7.13 y A.8.24). No constituye por sí mismo una certificación; valídelo con su auditor.",
        "La huella SHA-256 se calcula sobre los datos medidos (sin el JSON SMART) y permite verificar que no fueron alterados después de generarse el reporte.",
        "Los tamaños por categoría son tamaños lógicos de archivo. Pueden diferir del espacio usado real por clústeres, compresión, enlaces físicos (p. ej., WinSxS) y archivos protegidos; esa diferencia se muestra como 'No clasificado / Sin acceso'.",
        "Las pruebas de velocidad escriben un archivo temporal en la unidad y dependen de la carga del equipo durante la medición; repita la prueba para obtener valores comparables.",
        "Cuando no se dispone de datos SMART nativos, se usa el contador de confiabilidad de Windows y algunos parámetros pueden figurar como 'N/A'.",
    ]

    datos_excel = {
        "modo": modo_texto,
        "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "kpis": {"total_unidades": total_unidades, "nvme": nvme_count, "temp_max": max_temp if max_temp is not None else "N/D", "alertas": alertas_count,
                 "cap_total": suma_total, "cap_usado": suma_usado, "cap_libre": suma_libre},
        "destacados": [
            {"indicador": "Mayor lectura secuencial", "unidad": f"{mas_rapido_lectura['unidad']} - {mas_rapido_lectura['modelo']}",
             "valor": f"{mas_rapido_lectura['lectura_seq']} MB/s"},
            {"indicador": "Mayor escritura secuencial", "unidad": f"{mas_rapido_escritura['unidad']} - {mas_rapido_escritura['modelo']}",
             "valor": f"{mas_rapido_escritura['escritura_seq']} MB/s"},
            {"indicador": "Más estable térmicamente", "unidad": f"{mas_estable['unidad']} - {mas_estable['modelo']}" if mas_estable else "N/D",
             "valor": f"Δ {abs(mas_estable['temp_final'] - mas_estable['temp_inicial'])} °C" if mas_estable else "N/D"},
            {"indicador": "Almacenamiento más longevo", "unidad": f"{mas_longevo['unidad']} - {mas_longevo['modelo']}" if mas_longevo else "N/D",
             "valor": f"{mas_longevo['horas_encendido']} h" if mas_longevo else "N/D"},
        ] if registros else [],
        "auditoria": meta, "hallazgos": hallazgos_excel, "notas": notas_excel,
        "categorias_general": cat_general_excel if hay_cat else None,
        "fases": fases_labels,
        "timelines": [r["timeline_temps"] for r in registros],
        "unidades": [{
            "unidad": r["unidad"], "modelo": r["modelo"], "tecnologia": r["tecnologia"],
            "desgaste": "N/D" if r.get("sin_smart") else r["desgaste"],
            "horas": "N/D" if r.get("sin_smart") else r["horas_encendido"],
            "primer_uso": "N/D" if r.get("sin_smart") else r["fecha_primer_uso"],
            "temp_i": "N/D" if r.get("sin_smart") else r["temp_inicial"], "temp_f": "N/D" if r.get("sin_smart") else r["temp_final"],
            "lectura": r["lectura_seq"], "escritura": r["escritura_seq"],
            "iops": r["iops_aleatorio"], "latencia": r["latencia_ms"],
            "total_lecturas": r["total_lecturas"], "total_escrituras": r["total_escrituras"],
            "err_l": r["err_lectura"], "err_e": r["err_escritura"],
            "cap_total": r.get("total_gb", 0), "usado": r.get("usado_gb", 0), "libre": r.get("libre_gb", 0),
            "estado": r["estado_alerta"], "smart_json": r["smart_json_raw"],
            "fuente": r.get("fuente", "N/D"), "conformidad": r.get("conformidad", "N/D"),
            "n_hallazgos": len(r.get("hallazgos", [])), "inv": r.get("inventario", {}),
            "parametros": [{"parametro": a, "valor": b, "estado": c, "criterio": d} for a, b, c, d in parametros_smart(r)],
            "cat": filas_cat_unidad[i],
        } for i, r in enumerate(registros)],
    }
    excel_js = EXCEL_JS_TEMPLATE.replace("__DATA__", json.dumps(datos_excel, ensure_ascii=False).replace("</", "<\\/"))

    seccion_cat_html, cat_js = "", ""
    if hay_cat:
        alto_barras = max(288, 56 * len(registros) + 120)
        seccion_cat_html = f"""
        <!-- SEGMENTO 3: Uso por Categoría de Archivo -->
        <div class="mb-8">
            <div class="flex justify-between items-center mb-4">
                <h3 class="text-xl font-bold text-white">Uso por Categoría de Archivo</h3>
                <span class="text-xs text-slate-400 bg-slate-900 px-2 py-1 rounded">% sobre el espacio usado</span>
            </div>
            <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <div class="bg-slate-800/80 border border-slate-700/60 p-6 rounded-xl shadow-lg">
                    <h4 class="text-sm font-bold text-white mb-1">General (todas las unidades)</h4>
                    <p class="text-xs text-slate-400 mb-3">Distribución del espacio usado por tipo de archivo</p>
                    <div class="relative h-80"><canvas id="catGeneralChart"></canvas></div>
                </div>
                <div class="bg-slate-800/80 border border-slate-700/60 p-6 rounded-xl shadow-lg">
                    <h4 class="text-sm font-bold text-white mb-1">Por unidad</h4>
                    <p class="text-xs text-slate-400 mb-3">Cada barra suma 100% del espacio usado de la unidad</p>
                    <div class="relative" style="height: {alto_barras}px"><canvas id="catUnidadChart"></canvas></div>
                </div>
            </div>
        </div>
"""
        cat_js = f"""
        const CAT_NOMBRES = {json.dumps(cat_nombres)};
        const CAT_COLORES = {json.dumps(COLORES_CATEGORIAS)};
        const CAT_GENERAL_GB = {json.dumps(cat_general_gb)};
        const CAT_UNIDADES = {json.dumps(unidades_nombres)};
        const CAT_PCT = {json.dumps(cat_pct_unidad)};
        const CAT_GB = {json.dumps(cat_gb_unidad)};
        const tooltipBase = {{ backgroundColor: 'rgba(15, 23, 42, 0.9)', titleColor: '#f8fafc', bodyColor: '#cbd5e1',
                              borderColor: '#334155', borderWidth: 1, padding: 12 }};

        new Chart(document.getElementById('catGeneralChart').getContext('2d'), {{
            type: 'pie',
            data: {{ labels: CAT_NOMBRES, datasets: [{{ data: CAT_GENERAL_GB, backgroundColor: CAT_COLORES, borderColor: '#1e293b', borderWidth: 2 }}] }},
            options: {{
                responsive: true, maintainAspectRatio: false,
                plugins: {{
                    legend: {{ position: 'right', labels: {{ color: '#cbd5e1', boxWidth: 14, padding: 10 }} }},
                    tooltip: Object.assign({{}}, tooltipBase, {{ callbacks: {{ label: (c) => {{
                        const suma = CAT_GENERAL_GB.reduce((a, b) => a + b, 0);
                        const p = suma > 0 ? (c.parsed / suma * 100).toFixed(1) : '0.0';
                        return ` ${{c.label}}: ${{c.parsed.toLocaleString()}} GB (${{p}}%)`;
                    }} }} }})
                }}
            }}
        }});

        new Chart(document.getElementById('catUnidadChart').getContext('2d'), {{
            type: 'bar',
            data: {{
                labels: CAT_UNIDADES,
                datasets: CAT_NOMBRES.map((n, ci) => ({{
                    label: n, data: CAT_PCT.map(u => u[ci]), backgroundColor: CAT_COLORES[ci], borderColor: '#1e293b', borderWidth: 1
                }}))
            }},
            options: {{
                indexAxis: 'y', responsive: true, maintainAspectRatio: false,
                scales: {{
                    x: {{ stacked: true, max: 100, grid: {{ color: 'rgba(255, 255, 255, 0.05)' }},
                         ticks: {{ color: '#94a3b8', callback: (v) => v + '%' }} }},
                    y: {{ stacked: true, grid: {{ display: false }}, ticks: {{ color: '#94a3b8' }} }}
                }},
                plugins: {{
                    legend: {{ position: 'bottom', labels: {{ color: '#cbd5e1', boxWidth: 12, padding: 10 }} }},
                    tooltip: Object.assign({{}}, tooltipBase, {{ callbacks: {{ label: (c) =>
                        ` ${{c.dataset.label}}: ${{c.parsed.x}}% (${{CAT_GB[c.dataIndex][c.datasetIndex].toLocaleString()}} GB)` }} }})
                }}
            }}
        }});
"""

    html_content = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard Ejecutivo - Diagnóstico SMART y Analíticas</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/xlsx-js-style@1.2.0/dist/xlsx.bundle.js"></script>
</head>
<body class="bg-slate-900 text-slate-100 font-sans antialiased">
    <div class="max-w-7xl mx-auto px-4 py-8">
        
        <header class="mb-8 border-b border-slate-800 pb-6 flex flex-col md:flex-row justify-between items-start md:items-center">
            <div>
                <h1 class="text-3xl font-extrabold tracking-tight text-white">Dashboard de Rendimiento y Estabilidad SMART</h1>
                <p class="text-slate-400 mt-1">Modo: <span class="text-blue-400 font-semibold">{modo_texto}</span> | <span class="text-xs text-emerald-400 italic">💡 Motor nativo de Windows (sin dependencias)</span></p>
            </div>
            <div class="mt-4 md:mt-0 flex items-center gap-3 flex-wrap">
                <button onclick="exportarExcel()" class="bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold px-4 py-2.5 rounded-lg border border-emerald-500 shadow-lg transition-colors flex items-center gap-2">📥 Descargar Reporte Excel</button>
                <button onclick="resetFiltros()" class="bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-semibold px-4 py-2.5 rounded-lg border border-slate-700 transition-colors">🔄 Restablecer Filtros</button>
            </div>
        </header>

        <!-- Tarjetas KPI Principales -->
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
            <div class="bg-slate-800/80 border border-slate-700/60 p-5 rounded-xl shadow-lg">
                <p class="text-xs font-medium text-slate-400 uppercase tracking-wider">Total Unidades</p>
                <p class="text-3xl font-bold text-white mt-2">{total_unidades}</p>
            </div>
            <div class="bg-slate-800/80 border border-slate-700/60 p-5 rounded-xl shadow-lg">
                <p class="text-xs font-medium text-slate-400 uppercase tracking-wider">Unidades NVMe</p>
                <p class="text-3xl font-bold text-blue-400 mt-2">{nvme_count}</p>
            </div>
            <div class="bg-slate-800/80 border border-slate-700/60 p-5 rounded-xl shadow-lg">
                <p class="text-xs font-medium text-slate-400 uppercase tracking-wider">Temperatura Máxima</p>
                <p class="text-3xl font-bold text-amber-400 mt-2">{max_temp_txt}</p>
            </div>
            <div class="bg-slate-800/80 border border-slate-700/60 p-5 rounded-xl shadow-lg">
                <p class="text-xs font-medium text-slate-400 uppercase tracking-wider">Alertas Activas</p>
                <p class="text-3xl font-bold {'text-rose-500' if alertas_count > 0 else 'text-emerald-400'} mt-2">{alertas_count}</p>
            </div>
        </div>

        <!-- SECCIÓN ANALÍTICA EJECUTIVA (4 INDICADORES) -->
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
            <div class="bg-gradient-to-br from-blue-950/60 to-slate-900 border border-blue-500/30 p-5 rounded-xl shadow-lg">
                <div class="flex items-center gap-2 mb-2">
                    <span class="text-lg">🚀</span>
                    <h4 class="text-xs font-bold uppercase tracking-wider text-blue-400">Mayor Lectura</h4>
                </div>
                <p class="text-sm font-bold text-white truncate">{mas_rapido_lectura['unidad'] if mas_rapido_lectura else 'N/A'} - {mas_rapido_lectura['modelo'] if mas_rapido_lectura else ''}</p>
                <p class="text-2xl font-black text-blue-400 mt-1">{mas_rapido_lectura['lectura_seq'] if mas_rapido_lectura else 0} <span class="text-xs font-normal text-slate-400">MB/s</span></p>
            </div>

            <div class="bg-gradient-to-br from-emerald-950/60 to-slate-900 border border-emerald-500/30 p-5 rounded-xl shadow-lg">
                <div class="flex items-center gap-2 mb-2">
                    <span class="text-lg">⚡</span>
                    <h4 class="text-xs font-bold uppercase tracking-wider text-emerald-400">Mayor Escritura</h4>
                </div>
                <p class="text-sm font-bold text-white truncate">{mas_rapido_escritura['unidad'] if mas_rapido_escritura else 'N/A'} - {mas_rapido_escritura['modelo'] if mas_rapido_escritura else ''}</p>
                <p class="text-2xl font-black text-emerald-400 mt-1">{mas_rapido_escritura['escritura_seq'] if mas_rapido_escritura else 0} <span class="text-xs font-normal text-slate-400">MB/s</span></p>
            </div>

            <div class="bg-gradient-to-br from-amber-950/60 to-slate-900 border border-amber-500/30 p-5 rounded-xl shadow-lg">
                <div class="flex items-center gap-2 mb-2">
                    <span class="text-lg">🌡️</span>
                    <h4 class="text-xs font-bold uppercase tracking-wider text-amber-400">Más Estable Térmicamente</h4>
                </div>
                <p class="text-sm font-bold text-white truncate">{mas_estable['unidad'] if mas_estable else 'N/A'} - {mas_estable['modelo'] if mas_estable else ''}</p>
                <p class="text-lg font-bold text-amber-300 mt-1">Delta: {delta_txt}</p>
            </div>

            <div class="bg-gradient-to-br from-purple-950/60 to-slate-900 border border-purple-500/30 p-5 rounded-xl shadow-lg">
                <div class="flex items-center gap-2 mb-2">
                    <span class="text-lg">⏳</span>
                    <h4 class="text-xs font-bold uppercase tracking-wider text-purple-400">Almacenamiento Más Longevo</h4>
                </div>
                <p class="text-sm font-bold text-white truncate">{mas_longevo['unidad'] if mas_longevo else 'N/A'} - {mas_longevo['modelo'] if mas_longevo else ''}</p>
                <p class="text-lg font-bold text-purple-300 mt-1">{horas_long_txt} <span class="text-xs font-normal text-slate-400">Horas Activas</span></p>
            </div>
        </div>

        <!-- Gráficos Interactivos -->
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-8 mb-8">
            <div class="bg-slate-800/80 border border-slate-700/60 p-6 rounded-xl shadow-lg">
                <div class="flex justify-between items-center mb-4">
                    <h3 class="text-lg font-bold text-white">Evolución Térmica (Línea de Tiempo)</h3>
                    <span class="text-xs text-slate-400 bg-slate-900 px-2 py-1 rounded">Interactiva</span>
                </div>
                <div class="relative h-72">
                    <canvas id="tempChart"></canvas>
                </div>
            </div>
            <div class="bg-slate-800/80 border border-slate-700/60 p-6 rounded-xl shadow-lg">
                <div class="flex justify-between items-center mb-4">
                    <h3 class="text-lg font-bold text-white">Rendimiento Secuencial (MB/s)</h3>
                    <span class="text-xs text-slate-400 bg-slate-900 px-2 py-1 rounded">Interactiva</span>
                </div>
                <div class="relative h-72">
                    <canvas id="perfChart"></canvas>
                </div>
            </div>
        </div>

        <!-- SEGMENTO 1: Capacidad General -->
        <div class="mb-8">
            <div class="flex justify-between items-center mb-4">
                <h3 class="text-xl font-bold text-white">Capacidad General (todas las unidades)</h3>
                <span class="text-xs text-slate-400 bg-slate-900 px-2 py-1 rounded">Uso global: {porc_uso_general}%</span>
            </div>
            <div id="capGeneral"></div>
        </div>

        <!-- SEGMENTO 2: Capacidad por Unidad -->
        <div class="mb-8">
            <div class="flex justify-between items-center mb-4">
                <h3 class="text-xl font-bold text-white">Capacidad por Unidad</h3>
                <span class="text-xs text-slate-400 bg-slate-900 px-2 py-1 rounded">Usado vs Disponible</span>
            </div>
            <div id="capGrid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6"></div>
        </div>

{seccion_cat_html}
        <!-- Tabla y Filtros -->
        <div class="bg-slate-800/80 border border-slate-700/60 rounded-xl shadow-lg overflow-hidden mb-8">
            <div class="p-6 border-b border-slate-700/60 flex flex-col sm:flex-row justify-between items-center gap-4">
                <h3 class="text-lg font-bold text-white">Detalle Técnico de Unidades</h3>
                <div class="flex items-center gap-3 w-full sm:w-auto">
                    <input type="text" id="searchInput" placeholder="Buscar modelo o unidad..." class="bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-blue-500 w-full sm:w-64" onkeyup="filtrarTabla()">
                    <select id="techFilter" class="bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-blue-500" onchange="filtrarTabla()">
                        <option value="">Todas las Tecnologías</option>
                        <option value="NVMe">NVMe</option>
                        <option value="SSD">SSD</option>
                        <option value="HDD">HDD</option>
                    </select>
                </div>
            </div>
            
            <div class="overflow-x-auto">
                <table id="dataTable" class="w-full text-left border-collapse">
                    <thead>
                        <tr class="bg-slate-900/80 text-slate-400 text-xs uppercase tracking-wider border-b border-slate-700">
                            <th class="py-3 px-4">Unidad</th>
                            <th class="py-3 px-4">Modelo</th>
                            <th class="py-3 px-4">Tecnología</th>
                            <th class="py-3 px-4">Desgaste</th>
                            <th class="py-3 px-4">Horas de Uso</th>
                            <th class="py-3 px-4">Temp. Inicial</th>
                            <th class="py-3 px-4">Temp. Final</th>
                            <th class="py-3 px-4">Lectura Sec.</th>
                            <th class="py-3 px-4">Escritura Sec.</th>
                            <th class="py-3 px-4">IOPS 4K</th>
                            <th class="py-3 px-4">Latencia</th>
                            <th class="py-3 px-4">Estado</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-700/60 text-sm">
"""

    for r in registros:
        try:
            horas_txt = f"{int(r['horas_encendido']):,} h"
        except (TypeError, ValueError):
            horas_txt = "N/A"
        ns = bool(r.get("sin_smart"))
        desgaste_txt = "N/D" if ns else f"{r['desgaste']}%"
        t_ini_txt = "N/D" if ns else f"{r['temp_inicial']} °C"
        t_fin_txt = "N/D" if ns else f"{r['temp_final']} °C"
        if ns:
            horas_txt = "N/D"
        badge_color = ("bg-amber-500/20 text-amber-300 border border-amber-500/30" if r["estado_alerta"] == "SIN_SMART" else
                       "bg-emerald-500/20 text-emerald-400 border border-emerald-500/30" if r["estado_alerta"] == "ESTABLE" else
                       "bg-rose-500/20 text-rose-400 border border-rose-500/30")
        html_content += f"""
                        <tr class="hover:bg-slate-700/30 transition-colors">
                            <td class="py-3 px-4 font-bold text-white">{r['unidad']}</td>
                            <td class="py-3 px-4 text-slate-300">{r['modelo']}</td>
                            <td class="py-3 px-4"><span class="bg-slate-900 px-2 py-1 rounded text-xs border border-slate-700 font-semibold">{r['tecnologia']}</span></td>
                            <td class="py-3 px-4">{desgaste_txt}</td>
                            <td class="py-3 px-4">{horas_txt}</td>
                            <td class="py-3 px-4">{t_ini_txt}</td>
                            <td class="py-3 px-4 font-semibold text-amber-400">{t_fin_txt}</td>
                            <td class="py-3 px-4 text-blue-400 font-medium">{r['lectura_seq']} MB/s</td>
                            <td class="py-3 px-4 text-blue-400 font-medium">{r['escritura_seq']} MB/s</td>
                            <td class="py-3 px-4">{r['iops_aleatorio']}</td>
                            <td class="py-3 px-4">{r['latencia_ms']} ms</td>
                            <td class="py-3 px-4"><span class="px-2 py-1 rounded text-xs font-bold {badge_color}">{r['estado_alerta']}</span></td>
                        </tr>
"""

    html_content += """
                    </tbody>
                </table>
            </div>
        </div>

"""

    html_content += f"""
    </div>

    <script>
        function filtrarTabla() {{
            const input = document.getElementById("searchInput").value.toLowerCase();
            const filterTech = document.getElementById("techFilter").value.toUpperCase();
            const table = document.getElementById("dataTable");
            const tr = table.getElementsByTagName("tr");

            for (let i = 1; i < tr.length; i++) {{
                const tdUnidad = tr[i].getElementsByTagName("td")[0];
                const tdModelo = tr[i].getElementsByTagName("td")[1];
                const tdTech = tr[i].getElementsByTagName("td")[2];
                if (tdModelo && tdTech) {{
                    const txtUnidad = tdUnidad.textContent || tdUnidad.innerText;
                    const txtModelo = tdModelo.textContent || tdModelo.innerText;
                    const txtTech = tdTech.textContent || tdTech.innerText;
                    
                    const matchSearch = txtModelo.toLowerCase().indexOf(input) > -1 || txtUnidad.toLowerCase().indexOf(input) > -1;
                    const matchTech = filterTech === "" || txtTech.toUpperCase().indexOf(filterTech) > -1;
                    
                    if (matchSearch && matchTech) {{
                        tr[i].style.display = "";
                    }} else {{
                        tr[i].style.display = "none";
                    }}
                }}
            }}
        }}

        function resetFiltros() {{
            document.getElementById("searchInput").value = "";
            document.getElementById("techFilter").value = "";
            filtrarTabla();
            tempChart.data.datasets.forEach(d => d.hidden = false);
            tempChart.update();
            perfChart.data.datasets.forEach(d => d.hidden = false);
            perfChart.update();
        }}

        {excel_js}

        {cat_js}

        const ctxTemp = document.getElementById('tempChart').getContext('2d');
        const tempChart = new Chart(ctxTemp, {{
            type: 'line',
            data: {{
                labels: {json.dumps(fases_labels)},
                datasets: {json.dumps(datasets_termicos)}
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                interaction: {{ mode: 'index', intersect: false }},
                scales: {{
                    y: {{ grid: {{ color: 'rgba(255, 255, 255, 0.05)' }}, ticks: {{ color: '#94a3b8' }} }},
                    x: {{ grid: {{ color: 'rgba(255, 255, 255, 0.05)' }}, ticks: {{ color: '#94a3b8' }} }}
                }},
                plugins: {{ 
                    legend: {{ 
                        labels: {{ color: '#cbd5e1', boxWidth: 15, padding: 15 }},
                        onClick: function(e, legendItem, legend) {{
                            const index = legendItem.datasetIndex;
                            const ci = legend.chart;
                            const meta = ci.getDatasetMeta(index);
                            meta.hidden = meta.hidden === null ? !ci.data.datasets[index].hidden : null;
                            ci.update();
                        }}
                    }},
                    tooltip: {{
                        backgroundColor: 'rgba(15, 23, 42, 0.9)',
                        titleColor: '#f8fafc',
                        bodyColor: '#cbd5e1',
                        borderColor: '#334155',
                        borderWidth: 1,
                        padding: 12
                    }}
                }}
            }}
        }});

        const ctxPerf = document.getElementById('perfChart').getContext('2d');
        const perfChart = new Chart(ctxPerf, {{
            type: 'bar',
            data: {{
                labels: {json.dumps(unidades_nombres)},
                datasets: [
                    {{
                        label: 'Lectura Secuencial (MB/s)',
                        data: {json.dumps(seq_lectura)},
                        backgroundColor: 'rgba(59, 130, 246, 0.8)',
                        borderColor: 'rgba(59, 130, 246, 1)',
                        borderWidth: 1
                    }},
                    {{
                        label: 'Escritura Secuencial (MB/s)',
                        data: {json.dumps(seq_escritura)},
                        backgroundColor: 'rgba(16, 185, 129, 0.8)',
                        borderColor: 'rgba(16, 185, 129, 1)',
                        borderWidth: 1
                    }}
                ]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                scales: {{
                    y: {{ grid: {{ color: 'rgba(255, 255, 255, 0.05)' }}, ticks: {{ color: '#94a3b8' }} }},
                    x: {{ grid: {{ color: 'rgba(255, 255, 255, 0.05)' }}, ticks: {{ color: '#94a3b8' }} }}
                }},
                plugins: {{ 
                    legend: {{ 
                        labels: {{ color: '#cbd5e1', boxWidth: 15, padding: 15 }},
                        onClick: function(e, legendItem, legend) {{
                            const index = legendItem.datasetIndex;
                            const ci = legend.chart;
                            const meta = ci.getDatasetMeta(index);
                            meta.hidden = meta.hidden === null ? !ci.data.datasets[index].hidden : null;
                            ci.update();
                        }}
                    }},
                    tooltip: {{
                        backgroundColor: 'rgba(15, 23, 42, 0.9)',
                        titleColor: '#f8fafc',
                        bodyColor: '#cbd5e1',
                        borderColor: '#334155',
                        borderWidth: 1,
                        padding: 12
                    }}
                }}
            }}
        }});

        const capGeneral = {{ titulo: 'General', usado: {suma_usado}, libre: {suma_libre}, total: {suma_total} }};
        const capUnidades = {json.dumps([{"titulo": n, "usado": u, "libre": l, "total": t} for n, u, l, t in zip(unidades_nombres, cap_usado, cap_libre, cap_total)])};

        const COLOR_USADO = 'rgba(59, 130, 246, 0.85)';    // azul, igual que Lectura en el dashboard
        const COLOR_LIBRE = 'rgba(16, 185, 129, 0.85)';    // verde, igual que Escritura en el dashboard

        function crearPastel(canvas, d) {{
            return new Chart(canvas.getContext('2d'), {{
                type: 'pie',
                data: {{
                    labels: ['Total Usado', 'Total Disponible'],
                    datasets: [{{
                        data: [d.usado, d.libre],
                        backgroundColor: [COLOR_USADO, COLOR_LIBRE],
                        borderColor: '#1e293b',
                        borderWidth: 2
                    }}]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {{
                        legend: {{ position: 'bottom', labels: {{ color: '#cbd5e1', boxWidth: 15, padding: 15 }} }},
                        tooltip: {{
                            backgroundColor: 'rgba(15, 23, 42, 0.9)',
                            titleColor: '#f8fafc', bodyColor: '#cbd5e1',
                            borderColor: '#334155', borderWidth: 1, padding: 12,
                            callbacks: {{
                                label: (c) => {{
                                    const suma = d.usado + d.libre;
                                    const p = suma > 0 ? (c.parsed / suma * 100).toFixed(1) : '0.0';
                                    return ` ${{c.label}}: ${{c.parsed.toLocaleString()}} GB (${{p}}%)`;
                                }}
                            }}
                        }}
                    }}
                }}
            }});
        }}

        function el(tag, clase, texto) {{
            const e = document.createElement(tag);
            if (clase) e.className = clase;
            if (texto !== undefined) e.textContent = texto;
            return e;
        }}

        // --- Segmento 1: tarjeta general (pastel + indicadores) ---
        (function () {{
            const d = capGeneral;
            const pct = d.total > 0 ? (d.usado / d.total * 100).toFixed(1) : '0.0';
            const card = el('div', 'bg-slate-800/80 border border-slate-700/60 p-6 rounded-xl shadow-lg grid grid-cols-1 md:grid-cols-2 gap-6 items-center');

            const caja = el('div', 'relative h-72');
            const canvas = document.createElement('canvas');
            caja.appendChild(canvas);

            const kpis = el('div', 'grid grid-cols-1 sm:grid-cols-3 md:grid-cols-1 gap-4');
            [
                ['Almacenamiento Total', d.total.toLocaleString() + ' GB', 'text-white'],
                ['Total Usado', d.usado.toLocaleString() + ' GB (' + pct + '%)', 'text-blue-400'],
                ['Total Disponible', d.libre.toLocaleString() + ' GB', 'text-emerald-400']
            ].forEach(([etq, val, color]) => {{
                const k = el('div', 'bg-slate-900/60 border border-slate-700/60 p-4 rounded-lg');
                k.append(el('p', 'text-xs font-medium text-slate-400 uppercase tracking-wider', etq),
                         el('p', 'text-2xl font-bold mt-1 ' + color, val));
                kpis.appendChild(k);
            }});

            card.append(caja, kpis);
            document.getElementById('capGeneral').appendChild(card);
            crearPastel(canvas, d);
        }})();

        // --- Segmento 2: una tarjeta por unidad ---
        const capGrid = document.getElementById('capGrid');
        capUnidades.forEach((d) => {{
            const pct = d.total > 0 ? (d.usado / d.total * 100).toFixed(1) : '0.0';
            const card = el('div', 'bg-slate-800/80 border border-slate-700/60 p-5 rounded-xl shadow-lg');
            const caja = el('div', 'relative h-64');
            const canvas = document.createElement('canvas');
            caja.appendChild(canvas);
            card.append(
                el('h4', 'text-sm font-bold text-white truncate', d.titulo),
                el('p', 'text-xs text-slate-400 mb-3', 'Total: ' + d.total.toLocaleString() + ' GB · Usado: ' + pct + '%'),
                caja
            );
            capGrid.appendChild(card);
            crearPastel(canvas, d);
        }});
    </script>
</body>
</html>
"""

    if archivo_salida:
        with open(archivo_salida, "w", encoding="utf-8") as f:
            f.write(html_content)
    return html_content

INICIO_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Diagnóstico de Almacenamiento</title>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --borde:#334155; --txt:#f1f5f9; --sub:#94a3b8; --azul:#3b82f6; --verde:#10b981; --rojo:#f43f5e; --amb:#f59e0b; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--txt); font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  .wrap { max-width: 1100px; margin: 0 auto; padding: 24px 16px; }
  h1 { font-size: 1.6rem; margin: 0 0 4px; }
  .sub { color: var(--sub); font-size: .9rem; margin-bottom: 20px; }
  .card { background: var(--card); border: 1px solid var(--borde); border-radius: 12px; padding: 20px; margin-bottom: 16px; }
  h2 { font-size: 1rem; margin: 0 0 12px; color: #cbd5e1; text-transform: uppercase; letter-spacing: .05em; }
  label.op { display:flex; gap:10px; align-items:flex-start; padding:10px 12px; border:1px solid var(--borde); border-radius:8px; margin-bottom:8px; cursor:pointer; }
  label.op:hover { border-color: var(--azul); }
  label.op small { display:block; color: var(--sub); }
  .units { display:flex; flex-wrap:wrap; gap:8px; }
  .units label { border:1px solid var(--borde); border-radius:8px; padding:8px 12px; cursor:pointer; }
  button { font: inherit; border:0; border-radius:8px; padding:10px 18px; cursor:pointer; font-weight:600; color:#fff; }
  .primario { background: var(--azul); } .primario:hover { background:#2563eb; }
  .peligro { background: var(--rojo); } .neutro { background:#475569; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .barra { height: 14px; background:#0f172a; border-radius: 999px; overflow:hidden; border:1px solid var(--borde); }
  .barra > div { height:100%; width:0; background: linear-gradient(90deg, var(--azul), var(--verde)); transition: width .4s; }
  pre { background:#020617; color:#34d399; border:1px solid var(--borde); border-radius:8px; padding:12px; font-size:.78rem; max-height:260px; overflow:auto; margin:12px 0 0; white-space:pre-wrap; }
  .aviso { border-color: var(--amb); color:#fde68a; }
  .err { border-color: var(--rojo); color:#fecaca; }
  .fila { display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; align-items:center; }
  iframe { width:100%; height: 92vh; border:1px solid var(--borde); border-radius:12px; background: var(--bg); }
  .oculto { display:none; }
  a { color:#93c5fd; }
  a.btn { display:inline-block; text-decoration:none; color:#fff; border-radius:8px; padding:10px 18px; font-weight:600; }
  .punto { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; background:#64748b; }
  .punto.ok { background: var(--verde); } .punto.no { background: var(--rojo); }
  input[type=text], input[type=tel] { font: inherit; background:#0f172a; color:var(--txt); border:1px solid var(--borde); border-radius:8px; padding:10px 12px; }
  ol { margin:8px 0 0 18px; padding:0; color:#cbd5e1; font-size:.92rem; } li { margin-bottom:4px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Diagnóstico de Almacenamiento</h1>
  <div class="sub" id="equipo">Panel de control de pruebas de disco</div>
  <div class="sub"><span id="punto" class="punto"></span><span id="conexion">Buscando el agente en este equipo…</span></div>

  <div id="panelSinAgente" class="card aviso oculto">
    <h2 style="color:#fde68a">El agente local no está en ejecución</h2>
    <p style="margin:0 0 12px">Este archivo es el panel de control. Para medir los discos necesita el programa auxiliar en este equipo (un navegador no puede acceder al hardware por sí solo).</p>
    <a class="btn primario" href="diagnosticoalmacenamiento://iniciar">▶ Iniciar agente</a>
    <ol>
      <li>Pulsa <b>Iniciar agente</b> y acepta la apertura de la aplicación (y el aviso de Administrador de Windows).</li>
      <li>Si el botón no hace nada, ejecuta <b>una vez</b> <code>import_os_V3.py</code>: así se registra el acceso desde el navegador.</li>
      <li>Esta página se conecta sola en cuanto el agente esté listo.</li>
    </ol>
    <div style="margin-top:16px">
      <div class="sub" style="margin:0 0 6px">¿El agente está en <b>otro equipo</b> de tu red? Escribe su dirección (aparece en su ventana):</div>
      <div style="display:flex;gap:10px;flex-wrap:wrap">
        <input id="dirManual" type="text" placeholder="192.168.1.20:8765" style="min-width:220px">
        <button id="btnManual" class="neutro">Conectar</button>
      </div>
    </div>
  </div>

  <div id="panelLogin" class="card oculto">
    <h2>Acceso protegido</h2>
    <p style="margin:0 0 12px;color:#cbd5e1">Escribe el <b>PIN</b> que aparece en la ventana del agente, en el equipo donde se ejecuta.</p>
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <input id="pin" type="text" maxlength="12" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="PIN" style="width:200px;font-size:1.3rem;letter-spacing:.25em;text-align:center;text-transform:uppercase">
      <button id="btnPin" class="primario">Entrar</button>
    </div>
    <div id="pinError" class="sub" style="color:#fecaca;margin-top:8px"></div>
  </div>

  <div id="avisoAdmin" class="card aviso oculto">El programa no se está ejecutando como Administrador: algunas lecturas SMART pueden no estar disponibles.</div>

  <div id="panelConfig" class="card oculto">
    <h2>Nueva prueba</h2>
    <div style="margin-bottom:14px">
      <div class="sub" style="margin:0 0 6px">Unidades a analizar</div>
      <div class="units" id="unidades">Detectando unidades…</div>
    </div>
    <label class="op"><input type="radio" name="modo" value="1" checked>
      <span><b>Test General</b><small>Pruebas rápidas de lectura/escritura (1–3 min por unidad).</small></span></label>
    <label class="op"><input type="radio" name="modo" value="2">
      <span><b>Test Avanzado</b><small>Estrés térmico sostenido de ~2 minutos por unidad.</small></span></label>
    <label class="op"><input type="checkbox" id="cat" checked>
      <span><b>Analizar uso por categoría de archivos</b><small>Recorre la unidad; puede tardar varios minutos en discos grandes.</small></span></label>
    <div id="panelUltimo" class="sub oculto" style="margin-top:12px"><a href="#" id="lnkUltimo">Ver el último reporte generado</a></div>
    <div class="fila" style="margin-top:14px">
      <span class="sub" style="margin:0">Las pruebas escriben un archivo temporal en cada unidad y lo eliminan al terminar.</span>
      <button id="btnIniciar" class="primario" disabled>▶ Iniciar prueba</button>
    </div>
  </div>

  <div id="panelProgreso" class="card oculto">
    <div class="fila" style="margin-bottom:10px">
      <div><b id="progTitulo">En ejecución…</b><div class="sub" id="progEtapa" style="margin:2px 0 0"></div></div>
      <button id="btnCancelar" class="peligro">✖ Cancelar</button>
    </div>
    <div class="barra"><div id="barraFill"></div></div>
    <div class="sub" id="progPct" style="margin-top:6px">0%</div>
    <pre id="consola"></pre>
  </div>

  <div id="panelError" class="card err oculto"></div>

  <div id="panelResultado" class="oculto">
    <div class="fila" style="margin-bottom:10px">
      <span><b>Resultado</b> <span class="sub" id="rutaGuardado" style="margin:0 0 0 8px"></span></span>
      <span>
        <a id="lnkNueva" href="#" target="_blank" rel="noopener">Abrir en pestaña nueva ↗</a>
        &nbsp; <button id="btnOtra" class="neutro">↻ Nueva prueba</button>
      </span>
    </div>
    <iframe id="visor" title="Dashboard"></iframe>
  </div>
</div>

<script>
const TOKEN_FIJO = "__TOKEN__";   // solo viene relleno cuando el panel se abre en el propio equipo del agente
const PUERTOS = __PUERTOS__;
const $ = (id) => document.getElementById(id);
const CLAVE_DIR = "diag_dir", CLAVE_SES = "diag_sesion";
let BASE = location.protocol.startsWith("http") ? location.origin : null;
let token = TOKEN_FIJO;
let timer = null, vistaResultado = false, conectado = false, htmlActual = null, fallos = 0;

function mostrar(panel, visible) { $(panel).classList.toggle("oculto", !visible); }
function abortable(ms) { const c = new AbortController(); setTimeout(() => c.abort(), ms); return c.signal; }
function guardado(k) { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } }
function guardar(k, v) { try { v ? localStorage.setItem(k, v) : localStorage.removeItem(k); } catch (e) {} }

function cargarSesion() { token = TOKEN_FIJO || guardado(CLAVE_SES + "|" + BASE); }
function guardarSesion(t) { token = t; guardar(CLAVE_SES + "|" + BASE, t); }

async function api(ruta, op = {}) {
  if (!BASE) throw new Error("Sin agente");
  const r = await fetch(BASE + ruta, Object.assign({}, op, { headers: Object.assign({ "X-Token": token, "Content-Type": "application/json" }, op.headers || {}) }));
  let j = {}; try { j = await r.json(); } catch (e) {}
  if (!r.ok) { const e = new Error(j.error || r.statusText); e.auth = (r.status === 403); throw e; }
  return j;
}

async function detectar() {
  const manual = guardado(CLAVE_DIR);
  const candidatos = BASE ? [BASE] : [].concat(manual ? ["http://" + manual] : [], PUERTOS.map(p => "http://127.0.0.1:" + p));
  for (const b of candidatos) {
    try {
      const r = await fetch(b + "/api/ping", { signal: abortable(1500) });
      const j = await r.json();
      if (j.app === "diagnostico-almacenamiento") { BASE = b; return true; }
    } catch (e) {}
  }
  if (!location.protocol.startsWith("http")) BASE = null;
  return false;
}

function estadoConexion(ok) {
  conectado = ok;
  $("punto").className = "punto " + (ok ? "ok" : "no");
  $("conexion").textContent = ok ? "Agente conectado" : "Agente no detectado";
  mostrar("panelSinAgente", !ok);
  if (!ok) ["panelConfig", "panelProgreso", "avisoAdmin", "panelLogin"].forEach(x => mostrar(x, false));
}

function pedirPin() {
  clearTimeout(timer);
  ["panelConfig", "panelProgreso", "panelResultado", "avisoAdmin", "panelError"].forEach(x => mostrar(x, false));
  if (TOKEN_FIJO) {
    $("equipo").textContent = "Este panel no coincide con el agente (¿se regeneró diagnostico_config.json?). Abre el panel de nuevo desde " + BASE + "/";
    return;
  }
  mostrar("panelLogin", true); $("pin").focus();
}

async function loginPin() {
  const pin = $("pin").value.trim();
  $("pinError").textContent = "";
  try {
    const r = await fetch(BASE + "/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pin }) });
    let j = {}; try { j = await r.json(); } catch (e) {}
    if (!r.ok) throw new Error(j.error || "No se pudo iniciar sesión");
    guardarSesion(j.token); $("pin").value = "";
    await iniciarSesion();
  } catch (e) { $("pinError").textContent = e.message; }
}

async function iniciarSesion() {
  cargarSesion();
  try {
    await cargarInfo();
    mostrar("panelLogin", false); mostrar("panelConfig", !vistaResultado);
    sondear();
  } catch (e) { if (e.auth) pedirPin(); }
}

async function cargarInfo() {
  const i = await api("/api/info");
  $("equipo").textContent = `Equipo: ${i.equipo} · ${i.so}`;
  mostrar("avisoAdmin", !i.admin);
  const cont = $("unidades"); cont.textContent = "";
  if (!i.unidades.length) cont.textContent = "No se detectaron unidades fijas.";
  i.unidades.forEach(u => {
    const l = document.createElement("label");
    const c = document.createElement("input"); c.type = "checkbox"; c.checked = true; c.value = u.unidad;
    l.append(c, document.createTextNode(` ${u.unidad} (Disco ${u.disco})`));
    cont.appendChild(l);
  });
  $("btnIniciar").disabled = !i.unidades.length;
  mostrar("panelUltimo", !!i.ultimo_reporte);
}

async function iniciar() {
  const unidades = [...document.querySelectorAll("#unidades input:checked")].map(c => c.value);
  if (!unidades.length) { alert("Selecciona al menos una unidad."); return; }
  const modo = document.querySelector('input[name="modo"]:checked').value;
  try {
    await api("/api/iniciar", { method: "POST", body: JSON.stringify({ modo, categorias: $("cat").checked, unidades }) });
    vistaResultado = false; sondear();
  } catch (e) { if (e.auth) pedirPin(); else alert("No se pudo iniciar: " + e.message); }
}

async function mostrarReporte(ruta) {
  const r = await fetch(BASE + "/reporte", { headers: { "X-Token": token } });
  if (!r.ok) { alert("No hay un reporte disponible."); return; }
  htmlActual = await r.text();
  $("visor").srcdoc = htmlActual;
  $("lnkNueva").href = URL.createObjectURL(new Blob([htmlActual], { type: "text/html" }));
  $("rutaGuardado").textContent = ruta ? "Guardado en el equipo: " + ruta : "";
  vistaResultado = true;
  mostrar("panelResultado", true); mostrar("panelConfig", false); mostrar("panelProgreso", false);
}

function pintar(s) {
  const activo = s.estado === "corriendo" || s.estado === "cancelando";
  mostrar("panelConfig", !activo && !vistaResultado);
  mostrar("panelProgreso", activo);
  $("btnCancelar").disabled = s.estado === "cancelando";
  if (activo) {
    $("progTitulo").textContent = s.estado === "cancelando" ? "Cancelando…" : (s.progreso.unidad ? `Analizando ${s.progreso.unidad}` : "En ejecución…");
    $("progEtapa").textContent = s.progreso.etapa || "";
    $("barraFill").style.width = s.progreso.pct + "%";
    $("progPct").textContent = `${s.progreso.pct}% · unidad ${Math.min(s.progreso.idx + 1, s.progreso.total)} de ${s.progreso.total}`;
    const c = $("consola"); c.textContent = s.consola.join("\n"); c.scrollTop = c.scrollHeight;
  }
  mostrar("panelError", s.estado === "error" || s.estado === "cancelado");
  if (s.estado === "error") $("panelError").textContent = "La prueba falló: " + (s.error || "error desconocido");
  if (s.estado === "cancelado") $("panelError").textContent = "La prueba fue cancelada.";
  if (s.estado === "terminado" && !vistaResultado) mostrarReporte(s.ruta_reporte);
}

async function sondear() {
  clearTimeout(timer);
  try {
    const s = await api("/api/estado");
    pintar(s);
    if (s.estado === "corriendo" || s.estado === "cancelando") timer = setTimeout(sondear, 1000);
  } catch (e) { if (e.auth) pedirPin(); }
}

async function ciclo() {
  const ok = await detectar();
  if (ok && !conectado) {
    estadoConexion(true); fallos = 0;
    await iniciarSesion();
  } else if (!ok) {
    fallos++;
    if (!conectado || fallos >= 2) estadoConexion(false);
  } else { fallos = 0; }
  setTimeout(ciclo, conectado ? 4000 : 2000);
}

function conectarManual() {
  let v = $("dirManual").value.trim().replace(/^https?:\/\//, "").replace(/\/.*$/, "");
  if (!/^[A-Za-z0-9.\-]+(:\d{1,5})?$/.test(v)) { alert("Escribe una dirección como 192.168.1.20:8765"); return; }
  if (!v.includes(":")) v += ":" + PUERTOS[0];
  guardar(CLAVE_DIR, v);
  if (!location.protocol.startsWith("http")) BASE = null;
  conectado = false; $("conexion").textContent = "Buscando el agente…";
}

$("btnIniciar").onclick = iniciar;
$("btnCancelar").onclick = async () => { try { await api("/api/cancelar", { method: "POST", body: "{}" }); sondear(); } catch (e) { if (e.auth) pedirPin(); else alert(e.message); } };
$("btnOtra").onclick = () => { vistaResultado = false; mostrar("panelResultado", false); mostrar("panelError", false); mostrar("panelConfig", true); cargarInfo().catch(() => {}); };
$("lnkUltimo").onclick = (e) => { e.preventDefault(); mostrarReporte(""); };
$("btnPin").onclick = loginPin;
$("pin").addEventListener("keydown", (e) => { if (e.key === "Enter") loginPin(); });
$("btnManual").onclick = conectarManual;
$("dirManual").value = guardado(CLAVE_DIR);
ciclo();
</script>
</body>
</html>
"""

# ==========================================
# SERVIDOR WEB LOCAL (solo localhost)
# ==========================================
class PruebaCancelada(BaseException):
    """Se hereda de BaseException para que los 'except Exception' internos no la silencien."""

_CANCELAR = threading.Event()
PROGRESO = {"idx": 0, "total": 0, "frac": 0.0, "etapa": "", "unidad": ""}

def _verificar_cancelacion():
    if _CANCELAR.is_set():
        raise PruebaCancelada()

def _reportar(idx, total, frac, etapa, unidad=""):
    PROGRESO.update({"idx": idx, "total": total, "frac": frac, "etapa": etapa, "unidad": unidad})

class CapturaSalida:
    """Captura print()/sys.stdout.write del trabajo, respetando '\\r' de las barras de progreso."""
    def __init__(self, maximo=400):
        self.lineas, self.actual, self.maximo, self.lock = [], "", maximo, threading.Lock()
    def write(self, texto):
        with self.lock:
            for ch in texto:
                if ch == "\n":
                    self.lineas.append(self.actual); self.actual = ""
                    if len(self.lineas) > self.maximo:
                        del self.lineas[:len(self.lineas) - self.maximo]
                elif ch == "\r":
                    self.actual = ""
                else:
                    self.actual += ch
        return len(texto)
    def flush(self):
        pass
    def instantanea(self, n=25):
        with self.lock:
            return self.lineas[-n:] + ([self.actual] if self.actual else [])

class EstadoTrabajo:
    def __init__(self):
        self.lock = threading.Lock()
        self.estado = "inactivo"      # inactivo | corriendo | cancelando | terminado | cancelado | error
        self.error = ""
        self.html = None
        self.ruta = ""
        self.captura = CapturaSalida()

TRABAJO = EstadoTrabajo()

def ejecutar_analisis(modo_avanzado, analizar_cat, unidades_map, directorio_actual, modo_texto):
    """Pipeline completo. Devuelve el HTML del dashboard. Lo usan el modo web y el modo consola."""
    smartctl_bin = preparar_smartctl(directorio_actual)
    meta = obtener_metadatos_auditoria(modo_texto, analizar_cat)
    registros, total = [], len(unidades_map)
    for i, (unidad, disk_num) in enumerate(unidades_map):
        _verificar_cancelacion()
        print("-" * 82)
        print(f"[!] Analizando unidad {unidad} (Disco Físico ID: {disk_num})...")
        _reportar(i, total, 0.0, "Pruebas de rendimiento y lectura SMART", unidad)
        m = ejecutar_benchmark(unidad, disk_num, smartctl_bin, modo_avanzado)
        m["unidad"] = unidad
        _verificar_cancelacion()
        _reportar(i, total, 0.6, "Capacidad e inventario del activo", unidad)
        m.update(obtener_capacidad_unidad(unidad))
        m["inventario"] = obtener_info_auditoria(disk_num, unidad)
        if analizar_cat:
            _reportar(i, total, 0.7, "Analizando categorías de archivos", unidad)
            print(f"    -> Analizando categorías de archivos en {unidad} ...")
            m["categorias_info"] = analizar_categorias(raiz_analisis(unidad), m["usado_bytes"])
        m["hallazgos"], m["conformidad"] = evaluar_hallazgos(m)
        registros.append(m)
        _reportar(i, total, 1.0, "Unidad completada", unidad)
    _verificar_cancelacion()
    _reportar(total, total, 1.0, "Generando reporte")
    print("\n[!] Compilando reporte Dashboard HTML...")
    meta["fin"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    meta["duracion"] = f"{int(time.time() - meta['_t0'])} s"
    return generar_reporte_html(registros, None, modo_texto, meta)

def _trabajo_en_hilo(modo_avanzado, analizar_cat, unidades_map, directorio_actual):
    modo_texto = "Test Avanzado (Estrés Térmico Sostenido)" if modo_avanzado else "Test General (Modo Rápido)"
    try:
        with contextlib.redirect_stdout(TRABAJO.captura), contextlib.redirect_stderr(TRABAJO.captura):
            html = ejecutar_analisis(modo_avanzado, analizar_cat, unidades_map, directorio_actual, modo_texto)
        ruta = ""
        try:
            carpeta = os.path.join(directorio_actual, "reportes")
            os.makedirs(carpeta, exist_ok=True)
            ruta = os.path.join(carpeta, f"Dashboard_SMART_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.html")
            with open(ruta, "w", encoding="utf-8") as f:
                f.write(html)
        except OSError as e:
            escribir_log_depuracion(f"No se pudo guardar el reporte en disco: {e!r}")
            ruta = ""
        with TRABAJO.lock:
            TRABAJO.html, TRABAJO.ruta, TRABAJO.estado = html, ruta, "terminado"
    except PruebaCancelada:
        with TRABAJO.lock:
            TRABAJO.estado = "cancelado"
        TRABAJO.captura.write("\n[!] Prueba cancelada por el usuario.\n")
    except Exception as e:
        escribir_log_depuracion(f"Trabajo falló: {traceback.format_exc()}")
        with TRABAJO.lock:
            TRABAJO.estado, TRABAJO.error = "error", f"{type(e).__name__}: {e}"

def iniciar_trabajo(modo_avanzado, analizar_cat, unidades_map, directorio_actual):
    """Devuelve False si ya hay una prueba en curso (solo se permite una a la vez)."""
    with TRABAJO.lock:
        if TRABAJO.estado in ("corriendo", "cancelando"):
            return False
        TRABAJO.estado, TRABAJO.error, TRABAJO.html, TRABAJO.ruta = "corriendo", "", None, ""
        TRABAJO.captura = CapturaSalida()
    _CANCELAR.clear()
    _reportar(0, len(unidades_map), 0.0, "Preparando…")
    threading.Thread(target=_trabajo_en_hilo, args=(modo_avanzado, analizar_cat, unidades_map, directorio_actual),
                     daemon=True).start()
    return True

class ManejadorWeb(http.server.BaseHTTPRequestHandler):
    server_version = "Agente"
    sys_version = ""                 # no revelar versión de Python
    timeout = 15                     # corta conexiones lentas (slowloris)
    protocol_version = "HTTP/1.0"    # sin conexiones persistentes: una petición por conexión
    solo_lectura_remota = False      # True: los clientes remotos solo pueden ver, no iniciar/cancelar pruebas
    token = ""
    directorio = ""
    unidades = []          # [(letra, disco)]
    origenes_web = frozenset()   # páginas web (p. ej. GitHub Pages) autorizadas a controlar este agente
    permitir_origen_null = True  # el modo cliente lo desactiva
    pin = ""
    pin_un_uso = False     # True: el PIN actual es la clave de un solo uso con la que la página lanzó el agente
    sesiones = {}          # token de sesión -> {"ip": ..., "exp": ...}
    intentos = {}          # ip -> [fallos, bloqueado_hasta]
    fallos_globales = []   # marcas de tiempo de PIN incorrectos (límite global)
    candado = threading.Lock()
    DURACION_SESION = 12 * 3600      # 1 h cuando hay túnel de internet
    MAX_FALLOS_IP, BLOQUEO_IP = 5, 300
    MAX_FALLOS_GLOBAL, VENTANA_GLOBAL = 20, 600

    hosts_extra = frozenset()   # dominios públicos permitidos explícitamente (túneles); vacío = ninguno
    CABECERAS_PROXY = ("x-forwarded-for", "cf-connecting-ip", "x-real-ip", "forwarded", "cf-ray", "via", "x-forwarded-host")

    def _par_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _via_proxy(self):
        """Un túnel/proxy instalado en este mismo equipo conecta desde 127.0.0.1: NO es el usuario local."""
        return any(self.headers.get(c) for c in self.CABECERAS_PROXY)

    def _ip_cliente(self):
        """IP real del cliente. Solo se confía en X-Forwarded-For / CF-Connecting-IP si el par directo es el propio equipo."""
        if self._par_local() and self._via_proxy():
            reenviada = self.headers.get("CF-Connecting-IP") or (self.headers.get("X-Forwarded-For") or "").split(",")[0]
            reenviada = (reenviada or "").strip()
            return reenviada or "proxy-desconocido"
        return self.client_address[0]

    def _es_local(self):
        """Usuario realmente local: par loopback, sin cabeceras de proxy y entrando por localhost/127.0.0.1."""
        nombre = (self.headers.get("Host") or "").lower().rpartition(":")[0].strip("[]")
        return self._par_local() and not self._via_proxy() and nombre in ("localhost", "127.0.0.1", "::1")

    def _host_confiable(self, nombre):
        """Acepta localhost, IP literales, el nombre del equipo y los dominios autorizados con --host-permitido."""
        nombre = (nombre or "").strip("[]").lower()
        if nombre == "localhost" or nombre in self.hosts_extra:
            return True
        try:
            ipaddress.ip_address(nombre)
            return True
        except ValueError:
            pass
        mi = platform.node().lower()
        return bool(mi) and nombre in (mi, mi + ".local")

    def _host_valido(self):
        h = (self.headers.get("Host") or "").lower()
        nombre, sep, puerto = h.rpartition(":")
        if h.split(":")[0] in self.hosts_extra:      # túnel con dominio propio: llega sin puerto (HTTPS 443)
            return True
        return bool(sep) and puerto == str(self.server.server_address[1]) and self._host_confiable(nombre)

    def log_message(self, *args):   # silencia el log de accesos
        pass

    def _responder(self, codigo, cuerpo, tipo="application/json; charset=utf-8"):
        datos = cuerpo if isinstance(cuerpo, bytes) else cuerpo.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self._cabeceras_cors()
        self.end_headers()
        self.wfile.write(datos)

    def _origen_valido(self):
        """Sin Origin (navegación directa), 'null' (archivo abierto con doble clic) o una página servida por un agente."""
        origen = self.headers.get("Origin")
        if origen is None:
            return True
        if origen == "null":
            return self.permitir_origen_null
        if origen in self.origenes_web:
            return True
        try:
            p = urllib.parse.urlparse(origen)
            if (p.hostname or "").lower() in self.hosts_extra:
                return p.scheme in ("http", "https")
            return p.scheme == "http" and p.port == self.server.server_address[1] and self._host_confiable(p.hostname)
        except ValueError:
            return False

    def _cabeceras_cors(self):
        origen = self.headers.get("Origin")
        if origen and self._origen_valido():
            self.send_header("Access-Control-Allow-Origin", origen)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_OPTIONS(self):
        if not self._host_valido() or not self._origen_valido():
            return self._json(403, {"error": "No permitido"})
        self.send_response(204)
        self._cabeceras_cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, codigo, obj):
        if codigo in (401, 403, 429):
            registrar_acceso(f"DENEGADO_{codigo}", self._ip_cliente(), f"{self.command} {self.path.split('?')[0]}")
        self._responder(codigo, json.dumps(obj, ensure_ascii=False))

    def _autorizado(self, consulta):
        """Equipo local: token del panel. Otros equipos: token de sesión obtenido con el PIN, ligado a su IP."""
        if not self._host_valido() or not self._origen_valido():
            return False
        enviado = (self.headers.get("X-Token") or (consulta.get("t") or [""])[0]).strip()
        if not enviado:
            return False
        if self._es_local() and secrets.compare_digest(enviado.encode("utf-8", "ignore"), self.token.encode()):
            return True
        ahora = time.time()
        with self.candado:
            ses = self.sesiones.get(enviado)
            return bool(ses and ses["ip"] == self._ip_cliente() and ses["exp"] > ahora)

    def _login(self, datos):
        """Canjea el PIN por un token de sesión. Límite de intentos por IP y global (anti fuerza bruta)."""
        ip, ahora = self._ip_cliente(), time.time()
        with self.candado:
            self.fallos_globales[:] = [t for t in self.fallos_globales if ahora - t < self.VENTANA_GLOBAL]
            _, hasta = self.intentos.get(ip, [0, 0])
            if hasta > ahora or len(self.fallos_globales) >= self.MAX_FALLOS_GLOBAL:
                espera = int(max(hasta - ahora, 1))
                return self._json(429, {"error": f"Demasiados intentos. Reintenta en {espera} s."})
        pin = str(datos.get("pin", "")).strip().upper()
        correcto = secrets.compare_digest(pin.encode("utf-8", "ignore"), self.pin.encode())
        with self.candado:
            if correcto:
                if self.pin_un_uso:                      # la clave del lanzamiento desde la página se consume al usarse
                    ManejadorWeb.pin, ManejadorWeb.pin_un_uso = generar_pin(), False
                self.intentos.pop(ip, None)
                for k in [k for k, v in self.sesiones.items() if v["exp"] <= ahora]:
                    del self.sesiones[k]
                tok = secrets.token_urlsafe(24)
                self.sesiones[tok] = {"ip": ip, "exp": ahora + self.DURACION_SESION}
            else:
                fallos = self.intentos.get(ip, [0, 0])[0] + 1
                bloqueo = ahora + self.BLOQUEO_IP if fallos >= self.MAX_FALLOS_IP else 0
                self.intentos[ip] = [0 if bloqueo else fallos, bloqueo]
                self.fallos_globales.append(ahora)
        if correcto:
            registrar_acceso("LOGIN_OK", ip, "sesión remota iniciada", alerta=not self._es_local())
            return self._json(200, {"token": tok})
        registrar_acceso("LOGIN_FALLIDO", ip, "PIN incorrecto")
        self._json(401, {"error": "PIN incorrecto"})

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        consulta = urllib.parse.parse_qs(url.query)
        if not self._host_valido():
            return self._json(403, {"error": "Host no permitido"})
        if url.path == "/":
            # El token del panel solo se incrusta para el propio equipo; los demás deben ingresar el PIN
            return self._responder(200, renderizar_panel(self.token if self._es_local() else ""), "text/html; charset=utf-8")
        if url.path == "/api/ping":   # sin token: solo permite detectar que el agente existe
            if not self._origen_valido():
                return self._json(403, {"error": "Origen no permitido"})
            return self._json(200, {"app": "diagnostico-almacenamiento", "version": VERSION_SCRIPT,
                                    "sistema": SISTEMA, "smart": SMART_AUTORIZADO,
                                    "protocolo": protocolo_registrado()})
        if not self._autorizado(consulta):
            return self._json(403, {"error": "Token inválido"})
        if url.path == "/api/info":
            return self._json(200, {"equipo": platform.node(), "so": f"{platform.system()} {platform.release()}",
                                    "admin": bool(es_administrador()), "smart": SMART_AUTORIZADO, "sistema": SISTEMA,
                                    "version": VERSION_SCRIPT,
                                    "unidades": [{"unidad": u, "disco": d} for u, d in self.unidades],
                                    "ultimo_reporte": bool(TRABAJO.html or _ultimo_reporte_guardado(self.directorio))})
        if url.path == "/api/estado":
            with TRABAJO.lock:
                estado, error, ruta_rep = TRABAJO.estado, TRABAJO.error, TRABAJO.ruta
            p = dict(PROGRESO)
            total = max(1, p["total"])
            pct = int(max(0, min(1.0, (p["idx"] + p["frac"]) / total)) * 100)
            if estado == "terminado":
                pct = 100
            return self._json(200, {"estado": estado, "error": error, "ruta_reporte": ruta_rep, "consola": TRABAJO.captura.instantanea(),
                                    "progreso": {"idx": p["idx"], "total": p["total"], "pct": pct,
                                                 "etapa": p["etapa"], "unidad": p["unidad"]}})
        if url.path == "/reporte":
            with TRABAJO.lock:
                html = TRABAJO.html
            if not html:   # tras reiniciar el agente: último reporte guardado en disco
                ultimo = _ultimo_reporte_guardado(self.directorio)
                if ultimo:
                    with open(ultimo, "r", encoding="utf-8") as f:
                        html = f.read()
            if not html:
                return self._json(404, {"error": "Aún no hay un reporte disponible"})
            return self._responder(200, html, "text/html; charset=utf-8")
        self._json(404, {"error": "No encontrado"})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        consulta = urllib.parse.parse_qs(url.query)
        if not self._host_valido() or not self._origen_valido():
            return self._json(403, {"error": "No permitido"})
        es_login = url.path == "/api/login"
        if not es_login and not self._autorizado(consulta):
            return self._json(403, {"error": "No autorizado"})
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._json(415, {"error": "Se requiere application/json"})
        try:
            largo = int(self.headers.get("Content-Length") or 0)
            if largo > 4096:
                return self._json(413, {"error": "Solicitud demasiado grande"})
            datos = json.loads(self.rfile.read(largo) or b"{}")
            if not isinstance(datos, dict):
                raise ValueError("cuerpo no es un objeto")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "JSON inválido"})
        if es_login:
            return self._login(datos)

        if url.path == "/api/cerrar":
            with TRABAJO.lock:
                if TRABAJO.estado in ("corriendo", "cancelando"):
                    return self._json(409, {"error": "Hay una prueba en ejecución"})
            registrar_acceso("ACCION", self._ip_cliente(), "/api/cerrar")
            _CERRAR.set()
            return self._json(200, {"ok": True})
        if url.path in ("/api/iniciar", "/api/cancelar"):
            if self.solo_lectura_remota and not self._es_local():
                return self._json(403, {"error": "Este acceso remoto es de solo lectura"})
            registrar_acceso("ACCION", self._ip_cliente(), f"{url.path} {json.dumps(datos, ensure_ascii=False)[:200]}")
        if url.path == "/api/iniciar":
            modo = str(datos.get("modo"))
            if modo not in ("1", "2"):
                return self._json(400, {"error": "Modo inválido"})
            validas = {u for u, _ in self.unidades}
            pedidas = datos.get("unidades")
            if not isinstance(pedidas, list) or not pedidas or any(u not in validas for u in pedidas):
                return self._json(400, {"error": "Unidades inválidas"})
            seleccion = [(u, d) for u, d in self.unidades if u in pedidas]   # solo unidades locales detectadas
            if not iniciar_trabajo(modo == "2", bool(datos.get("categorias")), seleccion, self.directorio):
                return self._json(409, {"error": "Ya hay una prueba en ejecución"})
            return self._json(202, {"ok": True})
        if url.path == "/api/cancelar":
            with TRABAJO.lock:
                if TRABAJO.estado == "corriendo":
                    TRABAJO.estado = "cancelando"
                    _CANCELAR.set()
            return self._json(200, {"ok": True})
        self._json(404, {"error": "No encontrado"})

PUERTO_BASE = 8765
NOMBRE_PANEL = "Diagnostico_Almacenamiento.html"
PROTOCOLO = "diagnosticoalmacenamiento"

def _ultimo_reporte_guardado(directorio):
    carpeta = os.path.join(directorio, "reportes")
    try:
        archivos = [os.path.join(carpeta, f) for f in os.listdir(carpeta) if f.startswith("Dashboard_SMART_") and f.endswith(".html")]
        return max(archivos, key=os.path.getmtime) if archivos else None
    except OSError:
        return None

def cargar_token(directorio):
    """Token persistente (se genera una vez) para que el panel HTML guardado siga funcionando entre ejecuciones."""
    ruta = os.path.join(directorio, "diagnostico_config.json")
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            tok = json.load(f).get("token", "")
            if isinstance(tok, str) and len(tok) >= 20:
                return tok
    except (OSError, ValueError):
        pass
    tok = secrets.token_urlsafe(24)
    try:
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump({"token": tok}, f)
    except OSError as e:
        escribir_log_depuracion(f"No se pudo guardar el token: {e!r}")
    return tok

def renderizar_panel(token):
    puertos = list(range(PUERTO_BASE, PUERTO_BASE + 10))
    return INICIO_HTML.replace("__TOKEN__", token).replace("__PUERTOS__", json.dumps(puertos))

def escribir_panel_html(directorio, token):
    """Genera el panel de control como archivo independiente: se abre con doble clic desde cualquier carpeta."""
    ruta = os.path.join(directorio, NOMBRE_PANEL)
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(renderizar_panel(token))
    return ruta

def parametros_protocolo(argv=None):
    """Lee la URL diagnosticoalmacenamiento://iniciar?n=CLAVE&smart=1 pasada con --protocolo. Valida todo; ignora lo raro."""
    argv = sys.argv if argv is None else argv
    out = {}
    for i, a in enumerate(argv[:-1]):
        if a == "--protocolo":
            try:
                u = urllib.parse.urlparse(argv[i + 1])
                if u.scheme.lower() != PROTOCOLO:
                    return out
                q = urllib.parse.parse_qs(u.query)
                n = (q.get("n") or [""])[0]
                if re.fullmatch(r"[A-Za-z0-9_-]{16,64}", n):
                    out["n"] = n
                out["smart"] = (q.get("smart") or ["0"])[0] == "1"
            except ValueError:
                pass
    return out

def protocolo_registrado():
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{PROTOCOLO}\shell\open\command") as k:
            return os.path.abspath(comando_programa()[-1]) in winreg.QueryValueEx(k, "")[0]
    except Exception:
        return False

def quitar_protocolo():
    """Elimina el registro del protocolo (HKCU). Devuelve True si no queda nada registrado."""
    if os.name != "nt":
        return False
    try:
        import winreg
        raiz = rf"Software\Classes\{PROTOCOLO}"
        for sub in (r"\shell\open\command", r"\shell\open", r"\shell", ""):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, raiz + sub)
            except FileNotFoundError:
                pass
        return True
    except Exception as e:
        escribir_log_depuracion(f"No se pudo quitar el protocolo {PROTOCOLO}: {e!r}")
        return False

def registrar_protocolo(modo_cliente=False):
    """Registra diagnosticoalmacenamiento:// (HKCU, sin privilegios) para que una página pueda abrir el agente con un clic."""
    if os.name != "nt":
        return False
    try:
        import winreg
        comando = " ".join(f'"{x}"' for x in comando_programa()) + (' --cliente' if modo_cliente else '') + ' --protocolo "%1"'
        raiz = rf"Software\Classes\{PROTOCOLO}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, raiz) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:Diagnostico de Almacenamiento")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, raiz + r"\shell\open\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, comando)
        return True
    except Exception as e:
        escribir_log_depuracion(f"No se pudo registrar el protocolo {PROTOCOLO}: {e!r}")
        return False

def agente_en_ejecucion():
    """Devuelve el puerto de un agente ya iniciado (evita instancias duplicadas) o None."""
    for puerto in range(PUERTO_BASE, PUERTO_BASE + 10):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{puerto}/api/ping", timeout=1) as r:
                if json.loads(r.read()).get("app") == "diagnostico-almacenamiento":
                    return puerto
        except Exception:
            continue
    return None

NOMBRE_REGLA_FW = "Diagnostico Almacenamiento (agente)"

def comando_regla_firewall(puerto_inicial=8765):
    """Regla de entrada: solo red local (y Tailscale), solo perfiles Privado/Dominio, solo este Python."""
    return ["netsh", "advfirewall", "firewall", "add", "rule", f"name={NOMBRE_REGLA_FW}", "dir=in", "action=allow",
            "protocol=TCP", f"localport={puerto_inicial}-{puerto_inicial + 9}", "profile=private,domain",
            "remoteip=LocalSubnet,100.64.0.0/10", f"program={sys.executable}", "enable=yes"]

def firewall_regla_existe():
    try:
        r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={NOMBRE_REGLA_FW}"],
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0 and NOMBRE_REGLA_FW.lower() in r.stdout.lower()
    except Exception:
        return False

def perfil_de_red():
    """Categoría de las redes conectadas: 'Public' bloquea las conexiones entrantes."""
    try:
        out = ejecutar_comando_powershell("(Get-NetConnectionProfile | ForEach-Object { $_.NetworkCategory }) -join ','")
        return [x.strip() for x in out.split(",") if x.strip()]
    except Exception:
        return []

def asistente_red_local():
    """Comprueba Firewall y perfil de red (Windows) y ofrece corregirlos. Nada se cambia sin confirmación."""
    if os.name != "nt":
        return
    print("\n [Red] Comprobando que otros dispositivos puedan llegar a este equipo...")
    if not firewall_regla_existe():
        print("   - No existe una regla de Firewall para el agente. Sin ella, Windows suele bloquear las conexiones entrantes.")
        print("     Se crearía SOLO para redes Privadas/Dominio, solo desde tu red local, para este Python y los puertos 8765-8774.")
        resp = input("   ¿Crear la regla de Firewall ahora? [S/n]: ").strip().lower()
        if resp in ("", "s", "si", "sí", "y", "yes"):
            r = subprocess.run(comando_regla_firewall(PUERTO_BASE), capture_output=True, text=True)
            print("   ✔ Regla creada." if r.returncode == 0 else f"   ✘ No se pudo crear la regla: {(r.stdout + r.stderr).strip()[:200]}")
        else:
            print("   Omitido. Si no conectas desde otros dispositivos, vuelve a ejecutar y acepta.")
    else:
        print("   ✔ Regla de Firewall presente.")
    perfiles = perfil_de_red()
    if perfiles and any(x == "Public" for x in perfiles):
        print("   ⚠ Tu red está marcada como PÚBLICA: Windows bloquea las conexiones entrantes aunque exista la regla.")
        print("     Si es una red de confianza, cámbiala a Privada (PowerShell como administrador):")
        print("        Get-NetConnectionProfile            (anota el InterfaceIndex)")
        print("        Set-NetConnectionProfile -InterfaceIndex <N> -NetworkCategory Private")
    elif perfiles:
        print(f"   ✔ Perfil de red: {', '.join(perfiles)}")

_LOG_DEDUP = {}

def registrar_acceso(evento, ip, detalle="", alerta=False):
    """Bitácora de auditoría (accesos.log). Las denegaciones repetidas del mismo origen se agrupan (30 s)."""
    ahora = time.time()
    if evento.startswith("DENEGADO") or evento == "LOGIN_FALLIDO":
        clave = (evento, ip)
        if ahora - _LOG_DEDUP.get(clave, 0) < 30:
            return
        _LOG_DEDUP[clave] = ahora
    linea = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {evento} ip={ip} {detalle}\n"
    try:
        ruta = os.path.join(ManejadorWeb.directorio or ".", "accesos.log")
        if os.path.exists(ruta) and os.path.getsize(ruta) > 2_000_000:
            os.replace(ruta, ruta + ".1")
        with open(ruta, "a", encoding="utf-8") as f:
            f.write(linea)
    except OSError:
        pass
    if alerta:   # sys.__stdout__: la consola real, no la captura del trabajo
        try:
            sys.__stdout__.write(f"\n [ALERTA] Acceso remoto autorizado con PIN desde {ip} ({datetime.now().strftime('%H:%M:%S')})\n")
            sys.__stdout__.flush()
        except Exception:
            pass

def buscar_cloudflared(directorio):
    candidatos = [shutil.which("cloudflared"), os.path.join(directorio, "cloudflared.exe"),
                  r"C:\Program Files (x86)\cloudflared\cloudflared.exe", r"C:\Program Files\cloudflared\cloudflared.exe"]
    return next((c for c in candidatos if c and os.path.exists(c)), None)

class TunelRapido:
    """Lanza 'cloudflared tunnel --url' (túnel rápido gratuito) y captura la URL https://*.trycloudflare.com."""
    PATRON = re.compile(r"https://([a-z0-9][a-z0-9-]*)\.trycloudflare\.com")

    def __init__(self, binario, puerto):
        self.binario, self.puerto = binario, puerto
        self.proc, self.url, self.host, self.lineas = None, None, None, []
        self._listo = threading.Event()

    def _leer(self):
        for linea in self.proc.stdout:           # se sigue leyendo para no llenar la tubería
            self.lineas = (self.lineas + [linea.rstrip()])[-40:]
            m = self.PATRON.search(linea)
            if m and m.group(1) != "api" and not self.url:
                self.host = f"{m.group(1)}.trycloudflare.com"
                self.url = f"https://{self.host}/"
                self._listo.set()

    def iniciar(self, espera=45):
        flags = 0x08000000 if os.name == "nt" else 0       # CREATE_NO_WINDOW
        self.proc = subprocess.Popen([self.binario, "tunnel", "--url", f"http://127.0.0.1:{self.puerto}", "--no-autoupdate"],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                     errors="replace", creationflags=flags)
        threading.Thread(target=self._leer, daemon=True).start()
        self._listo.wait(espera)
        return self.url

    def detener(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except Exception:
                self.proc.kill()

_CERRAR = threading.Event()   # la página pide cerrar el agente al terminar

def servir(srv, tunel=None, horas=None, hilo=None):
    """Sirve en segundo plano hasta Ctrl+C o hasta cumplir el tiempo máximo; al salir cierra túnel y servidor."""
    if hilo is None:
        hilo = threading.Thread(target=srv.serve_forever, daemon=True)
        hilo.start()
    limite = time.time() + horas * 3600 if horas else None
    motivo = "detenido"
    try:
        while hilo.is_alive():
            if limite and time.time() >= limite:
                motivo = "tiempo máximo cumplido"
                break
            if tunel and tunel.proc and tunel.proc.poll() is not None:
                motivo = "el túnel se cerró"
                break
            if _CERRAR.wait(0.5):
                time.sleep(1)          # deja terminar de responder a la página
                motivo = "cerrado desde la página al terminar"
                break
    except KeyboardInterrupt:
        motivo = "detenido por el usuario"
    finally:
        if tunel:
            tunel.detener()
        srv.shutdown()
        srv.server_close()
    return motivo

def _valor_arg(nombre, defecto=None):
    for i, a in enumerate(sys.argv[:-1]):
        if a == nombre:
            return sys.argv[i + 1]
    return defecto

def ips_locales():
    """Direcciones IPv4 de este equipo en la red (para indicar la URL de acceso desde otros dispositivos)."""
    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("10.255.255.255", 1))   # no envía datos: solo elige la interfaz de salida
            ips.add(sk.getsockname()[0])
    except OSError:
        pass
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith(("127.", "169.254.")))

def generar_pin(largo_seguro=False):
    """6 dígitos en red local; 10 caracteres (sin ambiguos) si el agente es alcanzable por un túnel de internet."""
    if not largo_seguro:
        return f"{secrets.randbelow(10 ** 6):06d}"
    alfabeto = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
    return "".join(secrets.choice(alfabeto) for _ in range(10))

def crear_servidor(directorio, unidades, puerto_inicial=8765, token=None, host="0.0.0.0", pin=None, hosts_extra=()):
    """host='0.0.0.0': accesible desde la red local con PIN. host='127.0.0.1': solo este equipo."""
    for puerto in range(puerto_inicial, puerto_inicial + 10):
        try:
            srv = http.server.ThreadingHTTPServer((host, puerto), ManejadorWeb)
            ManejadorWeb.token = token or secrets.token_urlsafe(24)
            ManejadorWeb.hosts_extra = frozenset(h.lower().strip() for h in hosts_extra)
            ManejadorWeb.pin = (pin or generar_pin(bool(hosts_extra))).upper()
            ManejadorWeb.sesiones, ManejadorWeb.intentos, ManejadorWeb.fallos_globales = {}, {}, []
            ManejadorWeb.directorio = directorio
            ManejadorWeb.unidades = unidades
            srv.daemon_threads = True
            return srv, puerto
        except OSError:
            continue
    raise RuntimeError("No hay puertos libres entre 8765 y 8774.")

def _origenes_web(directorio):
    """Orígenes (páginas web) autorizados: constante del sitio + --origen-web + config. Solo https o localhost."""
    candidatos = list(ORIGENES_WEB_DEFECTO) + [sys.argv[i + 1] for i, a in enumerate(sys.argv[:-1]) if a == "--origen-web"]
    try:
        with open(os.path.join(directorio, "diagnostico_config.json"), "r", encoding="utf-8") as f:
            candidatos += list(json.load(f).get("origenes_web", []))
    except (OSError, ValueError):
        pass
    validos = set()
    for o in candidatos:
        o = str(o).strip().rstrip("/")
        p = urllib.parse.urlparse(o)
        if p.scheme == "https" and p.hostname or (p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1")):
            validos.add(f"{p.scheme}://{p.netloc}")
        else:
            print(f" [Aviso] Origen web ignorado (debe ser https o localhost): {o!r}")
    return validos

def main_web():
    global SMART_AUTORIZADO
    if "--desinstalar-protocolo" in sys.argv:
        print(" Acceso desde el navegador quitado." if quitar_protocolo() else " No se pudo quitar el registro (¿no es Windows?).")
        time.sleep(2)
        return
    cliente = "--cliente" in sys.argv
    proto = parametros_protocolo()
    SMART_AUTORIZADO = (not cliente) or ("--smart" in sys.argv) or bool(cliente and proto.get("smart"))   # en modo cliente, SMART solo con consentimiento explícito
    if ES_WINDOWS and SMART_AUTORIZADO and not es_administrador():
        print("[!] Solicitando privilegios de Administrador automáticamente...")
        elevar_privilegios()
        return
    directorio = carpeta_programa()
    desde_navegador = "--protocolo" in sys.argv
    print("=" * 82)
    print("   DIAGNÓSTICO DE ALMACENAMIENTO - AGENTE LOCAL")
    print("=" * 82)

    existente = agente_en_ejecucion()
    if existente:
        print(f"\n El agente ya está en ejecución (puerto {existente}). Puedes cerrar esta ventana.")
        if not desde_navegador:
            webbrowser.open(f"http://localhost:{existente}/")
        time.sleep(3)
        return

    print(" Detectando unidades...")
    unidades = obtener_unidades_y_discos_fisicos()
    if not unidades:
        print("[ERROR] No se detectaron unidades de disco fijas locales.")
        input("\nPresiona Enter para salir...")
        return

    token = cargar_token(directorio)
    tunel_rapido = "--tunel-rapido" in sys.argv
    solo_local = "--solo-local" in sys.argv or tunel_rapido or cliente     # con túnel o modo cliente: SOLO este equipo
    hosts_extra = [sys.argv[i + 1].lower() for i, a in enumerate(sys.argv[:-1]) if a == "--host-permitido"]
    try:
        horas = float(_valor_arg("--duracion-horas", 2 if tunel_rapido else (3 if cliente else 0))) or None
    except ValueError:
        print("[ERROR] --duracion-horas debe ser un número."); return

    binario = None
    if tunel_rapido:
        binario = buscar_cloudflared(directorio)
        if not binario:
            print("\n[ERROR] No se encontró 'cloudflared'. Instálalo (gratis) y vuelve a ejecutar:")
            print("   winget install --id Cloudflare.cloudflared")
            print("   (o descarga cloudflared.exe de https://github.com/cloudflare/cloudflared/releases y ponlo junto a este script)")
            input("\nPresiona Enter para salir...")
            return
        ManejadorWeb.DURACION_SESION = 3600
    ManejadorWeb.solo_lectura_remota = "--remoto-solo-lectura" in sys.argv
    ManejadorWeb.origenes_web = frozenset(_origenes_web(directorio))
    ManejadorWeb.permitir_origen_null = not cliente      # en modo cliente no se acepta el origen 'null' (archivos locales/iframes aislados)

    if not solo_local:
        asistente_red_local()
    srv, puerto = crear_servidor(directorio, unidades, PUERTO_BASE, token, "127.0.0.1" if solo_local else "0.0.0.0",
                                 pin=generar_pin(True) if (tunel_rapido or hosts_extra) else None, hosts_extra=hosts_extra)
    ruta_panel = escribir_panel_html(directorio, token)
    if cliente and proto.get("n"):
        ManejadorWeb.pin, ManejadorWeb.pin_un_uso = proto["n"].upper(), True
    registrado = registrar_protocolo(False) if not cliente else (registrar_protocolo(True) if "--registrar" in sys.argv else False)
    url = f"http://localhost:{puerto}/"
    print(f"\n Panel de control (archivo):  {ruta_panel}")
    print(f" Panel de control (este equipo): {url}")

    tunel, hilo_srv = None, None
    if tunel_rapido:
        print("\n Iniciando túnel rápido de Cloudflare (puede tardar unos segundos)...")
        hilo_srv = threading.Thread(target=srv.serve_forever, daemon=True)   # el túnel necesita el agente ya activo
        hilo_srv.start()
        tunel = TunelRapido(binario, puerto)
        if not tunel.iniciar():
            tunel.detener(); srv.shutdown(); srv.server_close()
            print("[ERROR] No se pudo obtener la URL del túnel. Últimas líneas de cloudflared:")
            for l in tunel.lineas[-8:]:
                print("   " + l)
            print("   Sugerencia: si existe ~/.cloudflared/config.yml, los túneles rápidos pueden no funcionar.")
            input("\nPresiona Enter para salir...")
            return
        ManejadorWeb.hosts_extra = frozenset({tunel.host})        # solo ESTE dominio es válido

    if cliente:
        sitio = URL_SITIO_DEFECTO or (sorted(ManejadorWeb.origenes_web)[0] if ManejadorWeb.origenes_web else "")
        print("\n " + "=" * 64)
        print("  MODO CLIENTE: este programa mide ESTE equipo y solo obedece a la página web autorizada")
        print(f"    Datos SMART: {'AUTORIZADOS (se leerán datos de salud de tus discos)' if SMART_AUTORIZADO else 'NO autorizados (solo velocidad, capacidad y categorías)'}")
        print(f"    Página autorizada: {', '.join(sorted(ManejadorWeb.origenes_web)) or '(ninguna: usa el panel local)'}")
        if ManejadorWeb.pin_un_uso:
            print("\n    Iniciado desde la página web: se conectará sola, no necesitas PIN.")
        else:
            print(f"\n    >>> PIN para la página web:  {ManejadorWeb.pin}  <<<")
        print(f"  Se cierra solo en {horas:g} h. Nada se envía a internet: la página habla con este programa en tu propio equipo.")
        print("  Para detenerlo: cierra esta ventana o presiona Ctrl+C.")
        print(" " + "=" * 64)
        if sitio and not desde_navegador:
            try:
                webbrowser.open(sitio)
            except Exception:
                pass
    elif tunel_rapido:
        print("\n " + "=" * 64)
        print("  ACCESO POR INTERNET (túnel rápido, cifrado HTTPS)")
        print(f"    URL:  {tunel.url}")
        print(f"    PIN:  {ManejadorWeb.pin}")
        print(f"  Se cierra automáticamente en {horas:g} h. Sesiones remotas: 1 h. Ctrl+C lo cierra ya.")
        print("  La URL y el PIN cambian en cada ejecución. Compártelos solo con quien deba entrar.")
        print("  Cada acceso remoto se anota en accesos.log y se avisa aquí.")
        print(" " + "=" * 64)
    elif solo_local:
        print(" Modo SOLO LOCAL: el agente no es accesible desde otros dispositivos.")
    else:
        print("\n " + "-" * 60)
        print("  ACCESO DESDE OTROS DISPOSITIVOS (misma red: PC, tablet, celular)")
        for ip in ips_locales():
            print(f"    Abre en el navegador:  http://{ip}:{puerto}/")
        print(f"    PIN de acceso:         {ManejadorWeb.pin}")
        print("  El PIN cambia cada vez que se inicia el agente. En este equipo no se pide.")
        for h in hosts_extra:
            print(f"    Dominio de túnel autorizado: {h}  (el PIN es más largo por ser accesible desde internet)")
        print("  Si Windows pregunta por el Firewall, permite el acceso en redes PRIVADAS.")
        print(" " + "-" * 60)
    if registrado:
        print(" Acceso desde el navegador registrado: la página podrá abrir este programa con un clic (solo tu usuario, sin administrador).")
        print(" Para quitarlo: ejecuta Quitar_acceso_navegador.bat.")
    print(" Mantén esta ventana abierta mientras uses el panel. Ctrl+C o cerrar la ventana detiene el agente.\n")
    if not desde_navegador and not cliente:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    motivo = servir(srv, tunel, horas, hilo_srv)
    print(f"\n[!] Agente finalizado: {motivo}.")

def main_consola():
    if ES_WINDOWS and not es_administrador():
        print("[!] Solicitando privilegios de Administrador automáticamente...")
        elevar_privilegios()
        return

    print("=" * 82)
    print("   BENCHMARK DE ALMACENAMIENTO - SELECCIÓN DE MODO")
    print("=" * 82)
    print(" Selecciona el tipo de prueba que deseas ejecutar:")
    print("   [1] Test General (Modo Rápido / Estándar)")
    print("   [2] Test Avanzado (Estrés Térmico Sostenido de ~2 minutos)")

    opcion = input("\n Ingresa el número de tu elección [1 o 2]: ").strip()
    modo_avanzado = (opcion == "2")
    modo_texto = "Test Avanzado (Estrés Térmico Sostenido)" if modo_avanzado else "Test General (Modo Rápido)"
    print(f"\n[!] Has seleccionado: {modo_texto}\n")

    resp_cat = input(" ¿Analizar el uso por categoría de archivos? (puede tardar varios minutos por unidad) [S/n]: ").strip().lower()
    analizar_cat = resp_cat in ("", "s", "si", "sí", "y", "yes")
    print()

    directorio_actual = carpeta_programa()
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    archivo_salida = os.path.join(directorio_actual, f"Dashboard_SMART_{timestamp}.html")

    unidades_map = obtener_unidades_y_discos_fisicos()
    if not unidades_map:
        print("[ERROR] No se detectaron unidades de disco fijas locales.")
        input("\nPresiona Enter para salir...")
        return

    try:
        html = ejecutar_analisis(modo_avanzado, analizar_cat, unidades_map, directorio_actual, modo_texto)
        with open(archivo_salida, "w", encoding="utf-8") as f:
            f.write(html)
        print("=" * 82)
        print("   REPORTE HTML CREADO CON ÉXITO")
        print("=" * 82)
        print(f"Archivo guardado en: {archivo_salida}\n")
        print(f"📝 Registro de depuración guardado en: {os.path.join(directorio_actual, 'smartctl_download_debug.log')}\n")
        print("[!] Abriendo automáticamente el Dashboard en tu navegador...")
        webbrowser.open("file://" + os.path.abspath(archivo_salida))
    except Exception as e:
        print(f"[ERROR] No se pudo generar el archivo HTML: {e}")

    input("\nPresiona Enter para cerrar la ventana...")

def main():
    if "--consola" in sys.argv:
        main_consola()
    else:
        main_web()

if __name__ == "__main__":
    main()
