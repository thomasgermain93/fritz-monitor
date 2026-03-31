import hashlib
import threading
import time
from xml.etree import ElementTree as ET

import requests
from flask import Flask, jsonify, render_template, request
from fritzconnection.core.fritzconnection import FritzConnection

app = Flask(__name__)

FRITZ_IP = '192.168.1.1'
FRITZ_USER = 'fritz1412'
FRITZ_PASSWORD = 'paper5192'
REFRESH_INTERVAL = 3

_web_sid = None
_web_sid_lock = threading.Lock()

_cache = {'devices': [], 'wan': {}, 'wan_history': [], 'error': None}
_lock = threading.Lock()


def get_web_sid():
    """Authentification MD5 sur l'interface web FritzBox."""
    global _web_sid
    r = requests.get(f'http://{FRITZ_IP}/login_sid.lua', timeout=5)
    root = ET.fromstring(r.text)
    sid = root.findtext('SID')
    challenge = root.findtext('Challenge')
    if sid == '0000000000000000':
        resp = hashlib.md5(f'{challenge}-{FRITZ_PASSWORD}'.encode('utf-16-le')).hexdigest()
        r2 = requests.get(f'http://{FRITZ_IP}/login_sid.lua',
                          params={'username': FRITZ_USER, 'response': f'{challenge}-{resp}'},
                          timeout=5)
        sid = ET.fromstring(r2.text).findtext('SID')
    with _web_sid_lock:
        _web_sid = sid
    return sid


def get_wan_stats():
    """
    Lit les débits DSL actuels via data.lua (valeurs en Kbps).
    downstream / upstream = débit actuel
    medium_downstream / medium_upstream = capacité ligne DSL
    Retourne les valeurs en bytes/s.
    """
    with _web_sid_lock:
        sid = _web_sid
    if not sid:
        sid = get_web_sid()

    r = requests.post(f'http://{FRITZ_IP}/data.lua',
                      data={'sid': sid, 'page': 'overview', 'xhr': '1', 'xhrId': 'all'},
                      timeout=6)

    if r.status_code == 403:
        sid = get_web_sid()
        r = requests.post(f'http://{FRITZ_IP}/data.lua',
                          data={'sid': sid, 'page': 'overview', 'xhr': '1', 'xhrId': 'all'},
                          timeout=6)

    conns = r.json().get('data', {}).get('internet', {}).get('connections', [])
    # Prend la connexion principale (DSL)
    main = next((c for c in conns if c.get('role') == 'main'), None) or (conns[0] if conns else {})

    def kbps_to_bps(kbps):
        return int(kbps) * 1000 // 8

    return {
        'down':        kbps_to_bps(main.get('downstream', 0)),
        'up':          kbps_to_bps(main.get('upstream', 0)),
        'max_down':    kbps_to_bps(main.get('medium_downstream', 0)),
        'max_up':      kbps_to_bps(main.get('medium_upstream', 0)),
        'provider':    main.get('provider', ''),
        'connected':   main.get('connected', False),
    }


def fmt_speed(bps):
    if bps >= 1_000_000:
        return f'{bps / 1_000_000:.1f} MB/s'
    if bps >= 1_000:
        return f'{bps / 1_000:.0f} KB/s'
    if bps > 0:
        return f'{bps} B/s'
    return '0'


def get_wlan_signal_map(fc):
    """Retourne {mac_upper: signal_strength} pour les devices WiFi associés."""
    signal_map = {}
    for wlan_num in [1, 2, 3]:
        try:
            result = fc.call_action(f'WLANConfiguration{wlan_num}', 'X_AVM-DE_GetWLANDeviceListPath')
            path = result.get('NewX_AVM-DE_WLANDeviceListPath', '')
            if not path:
                continue
            url = f'http://{FRITZ_IP}:49000{path}'
            r = fc.soaper.session.get(url, timeout=5)
            r.raise_for_status()
            root = ET.fromstring(r.text)
            for item in root.findall('Item'):
                mac = (item.findtext('AssociatedDeviceMACAddress') or '').upper()
                sig = item.findtext('X_AVM-DE_SignalStrength') or '0'
                if mac:
                    signal_map[mac] = int(sig)
        except Exception:
            pass
    return signal_map


def get_devices(fc):
    """Récupère la liste des appareils depuis le hostlist TR-064."""
    result = fc.call_action('Hosts1', 'X_AVM-DE_GetHostListPath')
    path = result.get('NewX_AVM-DE_HostListPath', '')
    if not path:
        raise RuntimeError('X_AVM-DE_GetHostListPath vide')

    url = f'http://{FRITZ_IP}:49000{path}'
    r = fc.soaper.session.get(url, timeout=5)
    r.raise_for_status()

    root = ET.fromstring(r.text)
    now = int(time.time())

    signal_map = get_wlan_signal_map(fc)

    devices = []
    for item in root.findall('Item'):
        iface = item.findtext('InterfaceType', '')
        if '802.11' in iface or 'WLAN' in iface.upper():
            iface_label = 'WiFi'
        elif iface:
            iface_label = 'LAN'
        else:
            iface_label = '?'

        active = item.findtext('Active', '0') == '1'
        mac = (item.findtext('MACAddress') or '').upper()
        priority = int(item.findtext('X_AVM-DE_Priority', '0') or 0)

        # Signal WiFi (0 si LAN)
        signal = signal_map.get(mac, 0) if iface_label == 'WiFi' else 0

        # WiFi link speed (Mbps) - pour info connexion, pas débit réel
        wifi_mbps = 0
        if iface_label == 'WiFi':
            raw_speed = int(item.findtext('X_AVM-DE_Speed', '0') or 0)
            wifi_mbps = raw_speed  # déjà en Mbps pour WiFi

        devices.append({
            'name': (item.findtext('HostName') or item.findtext('Name') or 'Inconnu').strip(),
            'ip': item.findtext('IPAddress', '') or '',
            'mac': mac,
            'interface': iface_label,
            'active': active,
            'priority': priority > 0,
            'signal': signal,           # 0-100 pour WiFi
            'wifi_mbps': wifi_mbps,     # débit négocié WiFi en Mbps
        })

    devices.sort(key=lambda d: (not d['active'], not d['priority'], -d['signal']))
    return devices


def poll_fritz():
    get_web_sid()  # auth initiale
    while True:
        try:
            fc = FritzConnection(address=FRITZ_IP, password=FRITZ_PASSWORD, timeout=5)
            devices = get_devices(fc)

            wan = {}
            try:
                raw = get_wan_stats()
                wan = {
                    'down':     raw['down'],
                    'up':       raw['up'],
                    'max_down': raw['max_down'],
                    'max_up':   raw['max_up'],
                    'down_fmt': fmt_speed(raw['down']),
                    'up_fmt':   fmt_speed(raw['up']),
                    'max_down_fmt': fmt_speed(raw['max_down']),
                    'max_up_fmt':   fmt_speed(raw['max_up']),
                    'provider': raw['provider'],
                    'connected': raw['connected'],
                }
            except Exception as e:
                print(f'[WAN] {e}')

            with _lock:
                _cache['devices'] = devices
                _cache['wan'] = wan
                _cache['error'] = None

                if wan:
                    _cache['wan_history'].append({
                        'down': wan['down'],
                        'up':   wan['up'],
                        't':    int(time.time()),
                    })
                    if len(_cache['wan_history']) > 60:
                        _cache['wan_history'] = _cache['wan_history'][-60:]

        except Exception as e:
            print(f'[poll] {e}')
            with _lock:
                _cache['error'] = str(e)

        time.sleep(REFRESH_INTERVAL)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/devices')
def api_devices():
    with _lock:
        return jsonify(_cache)


@app.route('/api/rename', methods=['POST'])
def api_rename():
    data = request.get_json()
    mac  = (data or {}).get('mac', '').strip()
    name = (data or {}).get('name', '').strip()
    if not mac or not name:
        return jsonify({'ok': False, 'error': 'mac et name requis'}), 400
    try:
        fc = FritzConnection(address=FRITZ_IP, password=FRITZ_PASSWORD, timeout=5)
        fc.call_action('Hosts1', 'X_AVM-DE_SetHostNameByMACAddress',
                       NewMACAddress=mac, NewHostName=name)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/priority', methods=['POST'])
def api_priority():
    data     = request.get_json()
    ip       = (data or {}).get('ip', '').strip()
    enabled  = bool((data or {}).get('enabled', False))
    if not ip:
        return jsonify({'ok': False, 'error': 'ip requis'}), 400
    try:
        fc = FritzConnection(address=FRITZ_IP, password=FRITZ_PASSWORD, timeout=5)
        fc.call_action('Hosts1', 'X_AVM-DE_SetPrioritizationByIP',
                       NewIPAddress=ip, **{'NewX_AVM-DE_Priority': enabled})
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


if __name__ == '__main__':
    t = threading.Thread(target=poll_fritz, daemon=True)
    t.start()
    print('Fritz Monitor → http://localhost:5000')
    app.run(host='0.0.0.0', port=5000, debug=False)
