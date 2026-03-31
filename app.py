import threading
import time
from xml.etree import ElementTree as ET

from flask import Flask, jsonify, render_template, request
from fritzconnection.core.fritzconnection import FritzConnection

app = Flask(__name__)

FRITZ_IP = '192.168.1.1'
FRITZ_USER = 'fritz1412'
FRITZ_PASSWORD = 'paper5192'
REFRESH_INTERVAL = 3

_cache = {'devices': [], 'wan': {}, 'wan_history': [], 'error': None}
_lock = threading.Lock()

# État pour le delta-calcul des débits
_prev_rx = 0
_prev_tx = 0
_prev_t  = 0.0

# Cache des capacités DSL (ne changent pas sauf re-sync)
_dsl_max = {'down': 0, 'up': 0, 'fetched_at': 0.0}
DSL_CACHE_TTL = 300  # re-lire toutes les 5 min


def get_wan_stats(fc):
    """
    Débits réels calculés par delta sur les compteurs cumulatifs 64-bit (GetAddonInfos).
    Fallback sur NewByteReceiveRate/NewByteSendRate si premier appel ou compteur incohérent.
    Capacité DSL max via GetCommonLinkProperties, cachée 5 min.
    """
    global _prev_rx, _prev_tx, _prev_t, _dsl_max

    addon = fc.call_action('WANCommonIFC1', 'GetAddonInfos')
    rx64  = int(addon.get('NewX_AVM_DE_TotalBytesReceived64') or 0)
    tx64  = int(addon.get('NewX_AVM_DE_TotalBytesSent64')     or 0)
    now   = time.time()

    # Calcul du débit par delta — plus précis que le taux interne FritzBox
    if _prev_t > 0 and rx64 >= _prev_rx and tx64 >= _prev_tx:
        dt   = now - _prev_t
        down = int((rx64 - _prev_rx) / dt) if dt > 0 else 0
        up   = int((tx64 - _prev_tx) / dt) if dt > 0 else 0
    else:
        # Premier appel ou compteur réinitialisé → taux FritzBox en fallback
        down = int(addon.get('NewByteReceiveRate') or 0)
        up   = int(addon.get('NewByteSendRate')    or 0)

    _prev_rx, _prev_tx, _prev_t = rx64, tx64, now

    # Capacité DSL — relire seulement si cache expiré
    if now - _dsl_max['fetched_at'] > DSL_CACHE_TTL:
        link = fc.call_action('WANCommonIFC1', 'GetCommonLinkProperties')
        _dsl_max['down']       = int(link.get('NewLayer1DownstreamMaxBitRate') or 0) // 8
        _dsl_max['up']         = int(link.get('NewLayer1UpstreamMaxBitRate')   or 0) // 8
        _dsl_max['fetched_at'] = now
        connected = link.get('NewPhysicalLinkStatus') == 'Up'
    else:
        connected = True  # on reçoit des données → connecté

    return {
        'down':      down,
        'up':        up,
        'max_down':  _dsl_max['down'],
        'max_up':    _dsl_max['up'],
        'connected': connected,
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

        # Débit négocié (Mbps) — WiFi et LAN
        raw_speed = int(item.findtext('X_AVM-DE_Speed', '0') or 0)
        wifi_mbps = raw_speed if iface_label == 'WiFi' else 0
        lan_mbps  = raw_speed if iface_label == 'LAN'  else 0

        devices.append({
            'name': (item.findtext('HostName') or item.findtext('Name') or 'Inconnu').strip(),
            'ip': item.findtext('IPAddress', '') or '',
            'mac': mac,
            'interface': iface_label,
            'active': active,
            'priority': priority > 0,
            'signal': signal,           # 0-100 pour WiFi
            'wifi_mbps': wifi_mbps,     # débit négocié WiFi en Mbps
            'lan_mbps':  lan_mbps,      # débit négocié LAN en Mbps
        })

    devices.sort(key=lambda d: (not d['active'], not d['priority'], -d['signal']))
    return devices


def poll_fritz():
    fc = None
    while True:
        try:
            if fc is None:
                print('[poll] Connexion FritzBox…')
                fc = FritzConnection(address=FRITZ_IP, user=FRITZ_USER,
                                     password=FRITZ_PASSWORD, timeout=10)
                print('[poll] Connecté.')

            devices = get_devices(fc)

            wan = {}
            try:
                raw = get_wan_stats(fc)
                wan = {
                    'down':     raw['down'],
                    'up':       raw['up'],
                    'max_down': raw['max_down'],
                    'max_up':   raw['max_up'],
                    'down_fmt': fmt_speed(raw['down']),
                    'up_fmt':   fmt_speed(raw['up']),
                    'max_down_fmt': fmt_speed(raw['max_down']),
                    'max_up_fmt':   fmt_speed(raw['max_up']),
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
            fc = None  # force reconnexion au prochain cycle
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
