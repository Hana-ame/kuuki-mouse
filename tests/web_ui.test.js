// web/ 手机端界面的逻辑测试 (键盘 + token 握手)。
//
// 为什么用 node + DOM 桩, 而不是浏览器里的测试框架:
// web/script.js 是一个 IIFE, 内部函数不导出, 也没有打包/模块化。要用它就得把源码读进来
// eval, 顺带把内部符号暴露出来 —— 这样测的是**真的那份代码**, 不是复制品。
//
// 从仓库根跑: node tests/web_ui.test.js
//
// 注入失败会直接报错退出 (不是静默通过): 如果 script.js 的 IIFE 结尾变了,
// 这里必须有人来看, 而不是让测试假装绿。
'use strict';

const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const SCRIPT = path.join(ROOT, 'web', 'script.js');

// ---------------- 最小 DOM 桩 ----------------
const allKeys = [];
function mkEl(tag) {
    const cls = new Set();
    const el = {
        tagName: tag, children: [], dataset: {}, style: {}, value: '', textContent: '',
        _cls: cls, _h: null,
        classList: {
            add: (c) => cls.add(c),
            remove: (c) => cls.delete(c),
            toggle: (c, on) => { if (on) cls.add(c); else cls.delete(c); },
            contains: (c) => cls.has(c),
        },
        appendChild(c) { this.children.push(c); if (cls.has('key')) allKeys.push(c); },
        addEventListener(ev, fn) { this._h = fn; },
        contains() { return false; },
        focus() {}, blur() {},
    };
    Object.defineProperty(el, 'className', {
        get: () => [...cls].join(' '),
        set: (v) => { cls.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => cls.add(c)); },
    });
    return el;
}
const els = {};
global.document = {
    getElementById: (id) => (els[id] = els[id] || mkEl('div')),
    createElement: mkEl,
    querySelectorAll: () => allKeys,
    activeElement: null,
};
global.window = { addEventListener() {}, DeviceMotionEvent: null, DeviceOrientationEvent: null };
global.location = { hash: '' };
global.history = { replaceState() {} };
// node 18+ 的 global.navigator 是只读 getter, 直接赋值在严格模式下会抛
Object.defineProperty(global, 'navigator', { value: {}, configurable: true, writable: true });

// ---------------- 假 PeerJS ----------------
// 目的: 拿到 conn, 手动触发 open/data/close, 观察握手行为
class FakeConn {
    constructor() { this.handlers = {}; this.sent = []; this.open = true; }
    on(ev, fn) { this.handlers[ev] = fn; }
    send(m) { this.sent.push(m); }
}
class FakePeer {
    constructor() { this.handlers = {}; FakePeer.last = this; }
    on(ev, fn) { this.handlers[ev] = fn; if (ev === 'open') fn(); }  // 同步触发, 好控制
    connect(id) { this.conn = new FakeConn(); FakePeer.lastConn = this.conn; return this.conn; }
    destroy() {}
}
global.Peer = FakePeer;

// ---------------- 载入被测代码并暴露内部符号 ----------------
const src = fs.readFileSync(SCRIPT, 'utf8');
const patched = src.replace(/\}\)\(\);\s*$/, `
    const __sent = [];
    send = function (msg) { __sent.push(msg); };
    globalThis.__t = {
        onKey, __sent, isUpper, labelOf, buildKeyboard, KEY_NAMES,
        connectPeer,
        setRoom(v) { room = v; },
        setToken(v) { token = v; },
        getAuthed: () => authed,
        getAuthFailed: () => authFailed,
        reset() { capsOn = false; shiftOn = false; mods = []; fnLayer = false; buildKeyboard(); },
    };
})();`);
if (patched === src) {
    console.error('注入失败: 没匹配到 web/script.js 的 IIFE 结尾 —— 测试无法进行, 请勿忽略');
    process.exit(1);
}
eval(patched);

const t = globalThis.__t;
const sent = t.__sent;
const click = (label) => t.onKey(label);
const last = () => sent[sent.length - 1];

let fail = 0;
function check(name, got, want) {
    const ok = JSON.stringify(got) === JSON.stringify(want);
    if (!ok) {
        fail += 1;
        console.log(`  FAIL ${name}\n       得到 ${JSON.stringify(got)}\n       期望 ${JSON.stringify(want)}`);
    } else {
        console.log(`  ok   ${name}`);
    }
}

// ---------------- 屏幕键盘 ----------------
console.log('--- 大小写 ---');
t.reset();
click('a'); check('小写 a', last(), { t: 'text', text: 'a' });
click('Shift'); click('a'); check('Shift+a = A', last(), { t: 'text', text: 'A' });
check('Shift 一次性用掉了', t.isUpper(), false);
click('Caps'); click('a'); check('Caps 锁定后 a = A', last(), { t: 'text', text: 'A' });
click('Shift'); check('Caps+Shift 反而小写 (与真键盘一致)', t.isUpper(), false);
click('a'); check('Caps+Shift 打 a', last(), { t: 'text', text: 'a' });

console.log('--- 修饰键 / 组合键 ---');
t.reset();
const n0 = sent.length;
click('Ctrl'); check('单按 Ctrl 不发送', sent.length - n0, 0);
click('c'); check('Ctrl+C', last(), { t: 'key', key: 'ctrl+c' });
click('c'); check('组合用完即清, 再按 c 是普通字符', last(), { t: 'text', text: 'c' });
click('Alt'); click('Tab'); check('Alt+Tab', last(), { t: 'key', key: 'alt+tab' });
click('Win'); click('␣'); check('Win+Space', last(), { t: 'key', key: 'cmd+space' });
// Caps 开着时 Ctrl+C 不能被大写污染 —— 组合键不分大小写
click('Caps'); click('Ctrl'); click('c');
check('Caps 开着时 Ctrl+C 仍是小写', last(), { t: 'key', key: 'ctrl+c' });

console.log('--- 特殊键 / Fn 层 ---');
t.reset();
click('Enter'); check('Enter', last(), { t: 'key', key: 'enter' });
click('⌫'); check('Backspace', last(), { t: 'key', key: 'backspace' });
click('Esc'); check('Esc', last(), { t: 'key', key: 'esc' });
click('Fn');
click('F5'); check('Fn 层 F5', last(), { t: 'key', key: 'f5' });
click('↑'); check('Fn 层方向键', last(), { t: 'key', key: 'up' });
click('PgUp'); check('Fn 层 PgUp', last(), { t: 'key', key: 'page_up' });
click('Ctrl'); click('F4'); check('Ctrl+F4', last(), { t: 'key', key: 'ctrl+f4' });
click('Fn'); click('a'); check('切回主层后 a 正常', last(), { t: 'text', text: 'a' });

// ---------------- token 握手 ----------------
// 主机设了 --token 时, 不先 auth 就发数据会被拒收并断开, 而且页面只会一直重连不说原因
console.log('--- token 握手 ---');
t.setRoom('ABCDE'); t.setToken('');
t.connectPeer();
const c1 = FakePeer.lastConn;
c1.handlers.open();
check('连上先发 auth (没填 token 也发)', c1.sent[0], { op: 'auth', args: { token: '' } });
check('auth 回包前不放行', t.getAuthed(), false);
c1.handlers.data({ ok: true, result: { authenticated: true } });
check('收到 authenticated 后放行', t.getAuthed(), true);

t.setToken('s3cret');
t.connectPeer();
const c2 = FakePeer.lastConn;
c2.handlers.open();
check('auth 带上填的 token', c2.sent[0], { op: 'auth', args: { token: 's3cret' } });

c2.handlers.data({ ok: false, error: { code: 'unauthorized', message: '需要 token' } });
check('token 不对 -> 标记失败', t.getAuthFailed(), true);
check('token 不对 -> 撤销放行', t.getAuthed(), false);

// ---------------- 键名与 Python 端对齐 ----------------
// 键摆得再标准, Python 端不认就是按不动 —— 两边必须对得上
console.log('--- 键名都能在 Python 端解析 ---');
const py = process.env.PY || 'python';
const probe = `import controller
names = ${JSON.stringify(Object.values(t.KEY_NAMES))}
missing = [n for n in names if len(n) != 1 and n.lower() not in controller.PynputMouseController.special_keys]
print('MISSING=' + ','.join(missing))
`;
try {
    const out = execFileSync(py, ['-c', probe], { encoding: 'utf8', cwd: ROOT });
    const missing = (out.match(/MISSING=(.*)/) || [, ''])[1].trim();
    if (missing) { fail += 1; console.log(`  FAIL Python 端查不到的键名: ${missing}`); }
    else console.log('  ok   所有键名 controller.special_keys 里都有');
} catch (e) {
    // 没有可用 Python 就跳过, 但要把话说出来, 别让人以为测过了
    console.log(`  skip Python 端键名检查 (跑不了 ${py}: ${e.message.split('\n')[0]})`);
}

// ---------------- 关键库不许再从 CDN 拉 ----------------
// unpkg 在国内经常连不上, 而 <script src> 阻塞渲染 —— 卡在 CDN 上就是白屏等超时。
// 所以 peerjs / mqtt 自托管在 web/vendor/, 文件必须真的在那儿 (光改 html 不够)。
console.log('--- 依赖自托管 ---');
const html = fs.readFileSync(path.join(ROOT, 'web', 'index.html'), 'utf8');
for (const lib of ['vendor/peerjs.min.js', 'vendor/mqtt.min.js']) {
    const onDisk = fs.existsSync(path.join(ROOT, 'web', lib));
    check(`${lib} 文件在且被引用`, onDisk && html.includes(lib), true);
}

console.log(fail ? `\n${fail} 项失败` : '\n全部通过');
process.exit(fail ? 1 : 0);
