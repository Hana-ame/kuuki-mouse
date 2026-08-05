// kuuki-mouse 手机遥控 (GitHub Pages)
// 配对: 扫 Python 端二维码 -> 打开本页 #/<房间码> -> PeerJS 直连 Python 主机
// 传输: PeerJS (0.peerjs.com 公开 broker, 主) + MQTT (HiveMQ 公开 broker, 兜底)
(() => {
    'use strict';

    const PEER_PREFIX = 'kuuki-mouse';   // 与 Python 端一致
    const MQTT_URL = 'wss://broker.hivemq.com:8884/mqtt';  // HiveMQ 公共 broker (my-node-app 同款)
    const MQTT_ROOT = 'kuuki-mouse';
    const SEND_INTERVAL_MS = 33;         // 约 30Hz 发送

    const $ = (id) => document.getElementById(id);
    const statusEl = $('status'), pairEl = $('pair'), controlEl = $('control');
    const sensorsEl = $('sensors'), chanEl = $('chan');

    // ---------------- 房间码 ----------------
    function roomFromHash() {
        const m = location.hash.match(/#\/([A-Za-z0-9]+)/);
        return m ? m[1].toUpperCase() : null;
    }
    let room = roomFromHash();
    if (room) $('roomInput').value = room;

    // ---------------- 传感器状态 ----------------
    let accel = { x: 0, y: 0, z: 0 };
    let orient = { alpha: 0, beta: 0, gamma: 0 };
    let rot = { gx: 0, gy: 0, gz: 0 };
    let hasSensor = false;
    let lastSend = 0;

    function onMotion(e) {
        const g = e.accelerationIncludingGravity;
        if (g && (g.x || g.y || g.z)) accel = { x: g.x, y: g.y, z: g.z };
        const r = e.rotationRate;
        if (r && r.alpha !== null && r.beta !== null && r.gamma !== null) {
            const k = Math.PI / 180;
            rot = { gx: r.alpha * k, gy: r.beta * k, gz: r.gamma * k }; // deg/s -> rad/s
        }
        hasSensor = true;
        renderSensors();
        sendSensor();
    }
    function onOrientation(e) {
        if (e.alpha !== null && e.beta !== null && e.gamma !== null) {
            orient = { alpha: e.alpha, beta: e.beta, gamma: e.gamma };
            renderSensors();
            sendSensor();
        }
    }
    function renderSensors() {
        $('accel').textContent = `(${accel.x.toFixed(1)}, ${accel.y.toFixed(1)}, ${accel.z.toFixed(1)})`;
        $('orient').textContent = `(${orient.alpha.toFixed(0)}, ${orient.beta.toFixed(0)}, ${orient.gamma.toFixed(0)})`;
        $('rot').textContent = `(${rot.gx.toFixed(2)}, ${rot.gy.toFixed(2)}, ${rot.gz.toFixed(2)})`;
    }

    // ---------------- 传输 (PeerJS + MQTT) ----------------
    let conn = null;
    let peer = null;
    let mqttc = null;
    let sendViaPeer = false;

    function send(msg) {
        const now = performance.now();
        if (msg.t === 'sensor' && now - lastSend < SEND_INTERVAL_MS) return;
        lastSend = now;
        const s = JSON.stringify(msg);
        if (sendViaPeer && conn && conn.open) {
            try { conn.send(msg); } catch (e) { /* ignore */ }
        }
        if (mqttc && mqttc.connected && room) {
            try { mqttc.publish(`${MQTT_ROOT}/${room}/data`, s, { qos: 0 }); } catch (e) { /* ignore */ }
        }
    }
    function sendSensor() {
        if (!room) return;
        send({ t: 'sensor', ...accel, ...orient, ...rot });
    }

    function connectPeer() {
        if (!room) return;
        if (peer) { try { peer.destroy(); } catch (e) {} }
        peer = new Peer();   // 随机 id, 连接 Python 主机
        peer.on('open', () => {
            chanEl.textContent = 'PeerJS 连接中...';
            conn = peer.connect(`${PEER_PREFIX}-${room}`, { serialization: 'json', reliable: false });
            conn.on('open', () => {
                sendViaPeer = true;
                statusEl.textContent = `已连接房间 ${room} (PeerJS)`;
                controlEl.classList.remove('hidden');
                sensorsEl.classList.remove('hidden');
                chanEl.textContent = 'PeerJS 直连';
            });
            conn.on('close', () => {
                sendViaPeer = false;
                statusEl.textContent = 'PeerJS 断开, 重连中...';
                chanEl.textContent = 'MQTT 兜底';
                setTimeout(connectPeer, 1500);
            });
            conn.on('error', () => {
                sendViaPeer = false;
                setTimeout(connectPeer, 1500);
            });
        });
        peer.on('error', (e) => {
            statusEl.textContent = `PeerJS 错误: ${e.type}`;
            setTimeout(connectPeer, 3000);
        });
    }

    function connectMqtt() {
        if (typeof mqtt === 'undefined') return;
        mqttc = mqtt.connect(MQTT_URL, {
            clientId: `kuuki-phone-${Math.random().toString(36).slice(2, 10)}`,
            clean: true, connectTimeout: 6000, reconnectPeriod: 3000,
        });
        mqttc.on('connect', () => {
            statusEl.textContent = 'MQTT 已连接, 等待主机...';
            mqttc.subscribe(`${MQTT_ROOT}/rooms/${room}`, { qos: 0 });   // 发现主机公告
            mqttc.on('message', (t, p) => {
                if (t.endsWith('/rooms/' + room)) {
                    const beat = JSON.parse(p.toString());
                    statusEl.textContent = `发现主机 ${beat.peerId} (MQTT 兜底通道可用)`;
                }
            });
        });
        mqttc.on('error', () => { /* HiveMQ 不可达时静默, PeerJS 仍可用 */ });
    }

    // ---------------- 控件 ----------------
    function sendControl(obj) { send({ ...obj }); }
    $('clickBtn').addEventListener('click', (e) => {
        e.stopPropagation();
        sendControl({ t: 'mouse', button: 'left' });
    });
    $('calibrateBtn').addEventListener('click', () => {
        sendControl({ t: 'calibrate' });
        statusEl.textContent = '已发送校准';
    });
    $('textInput').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            sendText();
        } else if (e.key === 'Backspace' && $('textInput').value === '') {
            e.preventDefault();
            sendControl({ t: 'key', key: 'Backspace' });
        }
    });
    $('sendBtn').addEventListener('click', sendText);

    function sendText() {
        const v = $('textInput').value;
        if (!v) { sendControl({ t: 'key', key: 'Enter' }); return; }  // 空则发回车
        sendControl({ t: 'text', text: v });
        $('textInput').value = '';
    }

    // ---------------- 权限 + 启动 ----------------
    async function requestPermission() {
        const need = [];
        if (window.DeviceMotionEvent && typeof DeviceMotionEvent.requestPermission === 'function') need.push('motion');
        if (window.DeviceOrientationEvent && typeof DeviceOrientationEvent.requestPermission === 'function') need.push('orientation');
        for (const k of need) {
            try {
                const r = await (k === 'motion'
                    ? DeviceMotionEvent.requestPermission()
                    : DeviceOrientationEvent.requestPermission());
                if (r !== 'granted') return false;
            } catch { return false; }
        }
        return true;
    }

    $('pairBtn').addEventListener('click', async () => {
        room = ($('roomInput').value || '').toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 8);
        if (!room) { statusEl.textContent = '请输入房间码'; return; }
        history.replaceState(null, '', `#/${room}`);
        const ok = await requestPermission();
        if (!ok) { statusEl.textContent = '传感器权限被拒绝'; return; }
        pairEl.classList.add('hidden');
        window.addEventListener('devicemotion', onMotion, true);
        window.addEventListener('deviceorientation', onOrientation, true);
        connectPeer();
        connectMqtt();
        statusEl.textContent = '正在连接...';
        sensorsEl.classList.remove('hidden');
    });

    // 从二维码打开时 (URL 已带房间码): 自动连接
    if (room) {
        // iOS 的 DeviceMotion/DeviceOrientation 权限必须在用户手势中请求,
        // 自动 click() 会被 Safari 拒权, 所以 iOS 上保留按钮让用户点一下。
        const needsGesture = (window.DeviceMotionEvent && typeof DeviceMotionEvent.requestPermission === 'function') ||
                             (window.DeviceOrientationEvent && typeof DeviceOrientationEvent.requestPermission === 'function');
        if (needsGesture) {
            $('roomInput').value = room;
            $('pairBtn').textContent = `配对 ${room}`;
            statusEl.textContent = `房间 ${room}: 点击开始配对`;
        } else {
            pairEl.classList.add('hidden');
            $('pairBtn').click();
        }
    }
})();