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

    // ---------------- 房间码 / token ----------------
    function roomFromHash() {
        const m = location.hash.match(/#\/([A-Za-z0-9]+)/);
        return m ? m[1].toUpperCase() : null;
    }
    // 二维码可以把 token 一并带上: #/ABCDE?token=xxx —— 手机扫码就不用手打
    function tokenFromHash() {
        const m = location.hash.match(/[?&]token=([^&]+)/);
        return m ? decodeURIComponent(m[1]) : '';
    }
    let room = roomFromHash();
    if (room) $('roomInput').value = room;
    let token = tokenFromHash();
    if (token) $('tokenInput').value = token;
    // 主机设了 --token 时, 不先 auth 就发数据会被拒收并断开 —— 连上先握手再说话
    let authed = false;
    let authFailed = false;   // token 不对时别再自动重连, 否则一直转圈且不说原因

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
    let inputMode = false;   // 输入模式: 禁用鼠标移动 (不发送传感器帧)
    let sentLen = 0;         // 文本框已发送长度
    let shiftOn = false;     // 屏幕键盘 Shift

    function send(msg) {
        const now = performance.now();
        if (msg.t === 'sensor' && now - lastSend < SEND_INTERVAL_MS) return;
        lastSend = now;
        const s = JSON.stringify(msg);
        // authed 之前不发: 主机设了 token 时, 抢在 auth 前发的数据会被当成未授权直接拒掉
        if (sendViaPeer && conn && conn.open && authed) {
            try { conn.send(msg); } catch (e) { /* ignore */ }
        }
        if (mqttc && mqttc.connected && room) {
            try { mqttc.publish(`${MQTT_ROOT}/${room}/data`, s, { qos: 0 }); } catch (e) { /* ignore */ }
        }
    }
    function sendSensor() {
        if (inputMode) return;   // 输入时禁用鼠标移动
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
                authed = false;
                // 无论主机有没有设 token 都先握手: 没设的话服务端照样回 authenticated,
                // 设了的话这一步就能拿到明确的"未授权"而不是被闷声断开
                try { conn.send({ op: 'auth', args: { token: token || '' } }); } catch (e) {}
                // 保险: 服务端不回 auth 包时别把通道卡死 (老版本不回这个响应)
                setTimeout(() => { if (!authed && !authFailed) authed = true; }, 1500);
                statusEl.textContent = `已连接房间 ${room} (PeerJS)`;
                controlEl.classList.remove('hidden');
                sensorsEl.classList.remove('hidden');
                chanEl.textContent = 'PeerJS 直连';
            });
            conn.on('data', (d) => {
                if (!d || typeof d !== 'object') return;
                if (d.ok && d.result && d.result.authenticated) {
                    authed = true;
                    authFailed = false;
                    chanEl.textContent = 'PeerJS 直连 (已鉴权)';
                } else if (d.error && d.error.code === 'unauthorized') {
                    authFailed = true;
                    authed = false;
                    chanEl.textContent = '未授权';
                    statusEl.textContent = '主机要求 token 且校验未通过 — 在配对区填对后重新配对';
                }
            });
            conn.on('close', () => {
                sendViaPeer = false;
                authed = false;
                if (authFailed) {
                    // 再连也只是再被拒一次, 而且用户看不出为什么 —— 停下来把原因说清楚
                    statusEl.textContent = 'token 未通过, 已停止重连 — 改对后点"开始配对"';
                    chanEl.textContent = '未授权';
                    return;
                }
                statusEl.textContent = 'PeerJS 断开, 重连中...';
                // 诚实一点: remote 受控端 (python -m remote / kuuki-agent.exe) 不订阅 MQTT,
                // 只有老版 main.py 收。写"兜底"会让人以为消息还有人接。
                chanEl.textContent = 'MQTT 兜底 (仅老版 main.py)';
                setTimeout(connectPeer, 1500);
            });
            conn.on('error', () => {
                sendViaPeer = false;
                if (!authFailed) setTimeout(connectPeer, 1500);
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

    function setInputMode(on) {
        inputMode = on;
        $('inputArea').classList.toggle('hidden', !on);
        $('modeBtn').textContent = on ? '🖱 鼠标' : '✍ 输入';
        $('status').textContent = on
            ? '输入模式: 鼠标移动已禁用, 打字直接进电脑'
            : (sendViaPeer ? `已连接房间 ${room} (PeerJS)` : '配对成功');
        if (!on) $('textInput').blur();
    }

    $('calibrateBtn').addEventListener('click', () => {
        setInputMode(false);
        sendControl({ t: 'calibrate' });
        $('status').textContent = '已发送校准';
    });
    $('modeBtn').addEventListener('click', () => {
        if (inputMode) setInputMode(false);
        else { setInputMode(true); $('textInput').focus(); }
    });

    // 鼠标按键: 左/中/右 + 滚轮上下
    $('clickBtn').addEventListener('click', () => { setInputMode(false); sendControl({ t: 'mouse', button: 'left' }); });
    $('midBtn').addEventListener('click', () => { setInputMode(false); sendControl({ t: 'mouse', button: 'middle' }); });
    $('rightBtn').addEventListener('click', () => { setInputMode(false); sendControl({ t: 'mouse', button: 'right' }); });
    $('wheelUpBtn').addEventListener('click', () => { setInputMode(false); sendControl({ t: 'scroll', delta: 1 }); });
    $('wheelDownBtn').addEventListener('click', () => { setInputMode(false); sendControl({ t: 'scroll', delta: -1 }); });

    // 输入模式: 文本框聚焦进入, 失焦(焦点离开输入区)退出
    $('textInput').addEventListener('focus', () => setInputMode(true));
    $('textInput').addEventListener('blur', () => {
        setTimeout(() => {
            const act = document.activeElement;
            if (!act || !$('inputArea').contains(act)) setInputMode(false);
        }, 0);
    });

    // 检测到任何字符 -> 立即发送 (拼音组合完成 / 普通按键输入都触发)
    function flushText() {
        const v = $('textInput').value;
        if (v.length > sentLen) {
            const newText = v.slice(sentLen);
            if (newText) sendControl({ t: 'text', text: newText });
        }
        sentLen = v.length;
    }
    $('textInput').addEventListener('input', (e) => { if (!e.isComposing) flushText(); });
    $('textInput').addEventListener('compositionend', flushText);
    $('textInput').addEventListener('keydown', (e) => {
        if (e.key === 'Backspace' && $('textInput').value === '') {
            e.preventDefault();
            sendControl({ t: 'key', key: 'Backspace' });   // 空框退格 = PC 退格
        }
    });

    // ---------------- 屏幕键盘: 标准 PC 布局 ----------------
    //
    // 按 ANSI 标准键盘排: Tab 在 Q 左边、Caps 在 A 左边、Shift 在 Z 左边、
    // Enter 在 L 右边、Backspace 在数字行右端 —— 手指记得住的位置不能错。
    // 之前那版 Tab 被塞在 Z 行末尾、整行没有 Caps, 看着像键盘用着不像。
    //
    // 两层: main = 主键区, fn = F1~F12 + 方向键/编辑键 (标准键盘也有这一层)。
    const KB_ROWS = {
        main: [
            ['Esc','1','2','3','4','5','6','7','8','9','0','-','=','⌫'],
            ['Tab','q','w','e','r','t','y','u','i','o','p','[',']','\\'],
            ['Caps','a','s','d','f','g','h','j','k','l',';',"'",'Enter'],
            ['Shift','z','x','c','v','b','n','m',',','.','/','Shift'],
            ['Ctrl','Win','Alt','␣','Alt','Fn'],
        ],
        fn: [
            ['Esc','F1','F2','F3','F4','F5','F6','F7','⌫'],
            ['F8','F9','F10','F11','F12','Ins','Del','Enter'],
            ['↑','↓','←','→','Home','End','PgUp','PgDn'],
            ['Ctrl','Win','Alt','␣','Alt','Fn'],
        ],
    };
    // 屏幕上的标签 -> 发给 Python 端的键名 (得过 controller.special_keys / resolve_key)
    const KEY_NAMES = {
        '⌫': 'backspace', '␣': 'space', 'Esc': 'esc', 'Enter': 'enter', 'Tab': 'tab',
        '↑': 'up', '↓': 'down', '←': 'left', '→': 'right',
        'Home': 'home', 'End': 'end', 'PgUp': 'page_up', 'PgDn': 'page_down',
        'Ins': 'insert', 'Del': 'delete', 'Win': 'cmd',
        'Ctrl': 'ctrl', 'Alt': 'alt',
    };
    for (let i = 1; i <= 12; i += 1) KEY_NAMES['F' + i] = 'f' + i;
    // 修饰键按标准键盘的相对宽度摆: 空格最宽, Enter/Shift 次之
    const KEY_FLEX = { Tab: 1.5, Caps: 1.8, Enter: 2.2, '⌫': 1.5, Shift: 2.0,
                       Ctrl: 1.3, Win: 1.3, Alt: 1.3, Fn: 1.3, '␣': 6 };

    let fnLayer = false;    // Fn 层 (F1~F12 / 方向键)
    let capsOn = false;     // Caps Lock: 锁定, 按一下一直大写
    let mods = [];          // 一次性组合修饰: 点 Ctrl 再点 C == Ctrl+C

    // Shift 与 Caps 是"异或"关系 —— 和真键盘一样: 都开着反而小写
    function isUpper() { return capsOn !== shiftOn; }
    function labelOf(raw) { return (/^[a-z]$/.test(raw) && isUpper()) ? raw.toUpperCase() : raw; }

    function sendWithMods(name) {
        // 组合键里的字母统一小写: Ctrl+C 和 Ctrl+c 是同一个键, 不能让 Caps/Shift
        // 的当前状态渗进来 (开着 Caps 时点 Ctrl+C 会发出 "ctrl+C", 语义上是脏的)
        const norm = (name.length === 1) ? name.toLowerCase() : name;
        if (mods.length) {
            sendControl({ t: 'key', key: mods.concat(norm).join('+') });  // ctrl+c
            mods = [];
        } else {
            sendControl({ t: 'key', key: name });
        }
        renderKeys();
    }

    function onKey(raw) {
        setInputMode(true);
        // 修饰键本身不发送, 只改状态 (真键盘上单按 Ctrl 也不会输出字符)
        if (raw === 'Shift') { shiftOn = !shiftOn; renderKeys(); return; }
        if (raw === 'Caps') { capsOn = !capsOn; renderKeys(); return; }
        if (raw === 'Fn') { fnLayer = !fnLayer; buildKeyboard(); return; }
        if (raw === 'Ctrl' || raw === 'Alt' || raw === 'Win') {
            const m = KEY_NAMES[raw];
            const at = mods.indexOf(m);
            if (at >= 0) mods.splice(at, 1); else mods.push(m);
            renderKeys();
            return;
        }
        const name = KEY_NAMES[raw];
        if (name) { sendWithMods(name); return; }
        // 普通字符: 有修饰键在就走组合, 否则按 text 发 (支持中文输入法那套逻辑)
        if (mods.length) { sendWithMods(labelOf(raw)); return; }
        sendControl({ t: 'text', text: labelOf(raw) });
        if (shiftOn) { shiftOn = false; }   // Shift 是一次性的, Caps 不是
        renderKeys();
    }

    function renderKeys() {
        document.querySelectorAll('#keyboard .key').forEach((b) => {
            const raw = b.dataset.raw;
            b.textContent = labelOf(raw);
            const on = (raw === 'Shift' && shiftOn) || (raw === 'Caps' && capsOn)
                    || (raw === 'Fn' && fnLayer) || mods.indexOf(KEY_NAMES[raw]) >= 0;
            b.classList.toggle('key-on', !!on);
        });
    }

    function buildKeyboard() {
        const kb = $('keyboard');
        kb.textContent = '';
        KB_ROWS[fnLayer ? 'fn' : 'main'].forEach((row) => {
            const r = document.createElement('div');
            r.className = 'kbd-row';
            row.forEach((k) => {
                const b = document.createElement('button');
                b.className = 'key';
                b.dataset.raw = k;
                if (k === '␣') b.classList.add('key-space');
                else if (k.length > 1 || k === '⌫') b.classList.add('key-fn');
                if (KEY_FLEX[k]) b.style.flex = String(KEY_FLEX[k]);
                b.addEventListener('click', () => onKey(k));
                r.appendChild(b);
            });
            kb.appendChild(r);
        });
        renderKeys();
    }
    buildKeyboard();

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
        token = ($('tokenInput').value || '').trim();
        authFailed = false;   // 改过 token 就值得再试一次
        // token 要留在地址栏里: 手机刷新页面是常事, 抹掉的话每次都得重打一遍口令
        history.replaceState(
            null, '',
            token ? `#/${room}?token=${encodeURIComponent(token)}` : `#/${room}`
        );
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