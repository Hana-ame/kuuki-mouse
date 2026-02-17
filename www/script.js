document.addEventListener('DOMContentLoaded', () => {
    // --- UI 元素 ---
    const mouseBtn = document.getElementById('mouseBtn');
    const inputBtn = document.getElementById('inputBtn');
    const inputOverlay = document.getElementById('inputOverlay');
    const hiddenInput = document.getElementById('hiddenInput');
    const cancelBtn = document.getElementById('cancelBtn');
    const sendBtn = document.getElementById('sendBtn');
    const statusDot = document.getElementById('statusDot');
    const statusText = document.getElementById('statusText');
    const rowNum = document.getElementById('row-num'); // 数字行容器
    const tabBtns = document.querySelectorAll('.tab-btn');
    const pages = document.querySelectorAll('.page');
    const keyboardContainer = document.querySelector('.keyboard-container');
    const btnToggleSymbol = document.getElementById('btnToggleSymbol');

    // --- 状态 ---
    let socket;
    let isMouseOn = false;
    let isSymbolMode = false; // 数字/符号切换状态
    const activeModifiers = new Set();

    // --- 按键映射定义 ---
    const numbers = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'];
    const symbols = ['!', '@', '#', '$', '%', '^', '&', '*', '(', ')'];

    // --- 初始化数字行 ---
    function renderNumRow() {
        rowNum.innerHTML = '';
        const dataSource = isSymbolMode ? symbols : numbers;
        dataSource.forEach(char => {
            const btn = document.createElement('button');
            btn.className = 'key';
            btn.textContent = char;
            btn.dataset.key = char;
            rowNum.appendChild(btn);
        });
        // 数字行不再放退格键，保持纯净的10个键
    }
    renderNumRow();

    // --- WebSocket ---
    function connect() {
        socket = new WebSocket('wss://' + location.host + '/ws');
        socket.onopen = () => { statusDot.classList.add('on'); statusText.textContent = '已连接'; };
        socket.onclose = () => { statusDot.classList.remove('on'); statusText.textContent = '断开'; setTimeout(connect, 1000); };
    }
    connect();

    function send(data) {
        if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(data));
    }

    // --- 1. 标签页切换 ---
    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            tabBtns.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            
            const target = btn.dataset.tab;
            pages.forEach(p => {
                p.classList.remove('active');
                if(p.id === `page-${target}`) p.classList.add('active');
            });
        });
    });

    // --- 2. 鼠标逻辑 ---
    function handleOrientation(e) {
        if (!isMouseOn) return;
        send({ x: 0, y: 0, z: 0, alpha: e.alpha, beta: e.beta, gamma: e.gamma });
    }
    
    mouseBtn.addEventListener('click', async () => {
        if (isMouseOn) {
            isMouseOn = false;
            mouseBtn.textContent = '鼠标:关';
            mouseBtn.classList.remove('active');
            window.removeEventListener('deviceorientation', handleOrientation);
        } else {
            let granted = true;
            if (typeof DeviceOrientationEvent !== 'undefined' && typeof DeviceOrientationEvent.requestPermission === 'function') {
                try { if (await DeviceOrientationEvent.requestPermission() !== 'granted') granted = false; } 
                catch (e) { granted = false; console.error(e); }
            }
            if (granted) {
                isMouseOn = true;
                mouseBtn.textContent = '鼠标:开';
                mouseBtn.classList.add('active');
                window.addEventListener('deviceorientation', handleOrientation, true);
            } else { alert('需要陀螺仪权限'); }
        }
    });

    // --- 3. 文字输入逻辑 ---
    inputBtn.addEventListener('click', () => { inputOverlay.classList.add('visible'); hiddenInput.focus(); });
    cancelBtn.addEventListener('click', () => { inputOverlay.classList.remove('visible'); hiddenInput.blur(); });
    sendBtn.addEventListener('click', () => {
        if(hiddenInput.value) { send({ text: hiddenInput.value }); hiddenInput.value = ''; }
        cancelBtn.click();
    });

    // --- 4. 虚拟键盘核心逻辑 ---
    
    function clearModifiers() {
        activeModifiers.forEach(k => {
            send({ keyup: k });
            const btn = document.querySelector(`.key-modifier[data-key="${k}"]`);
            if(btn) btn.classList.remove('active');
        });
        activeModifiers.clear();
    }

    // 事件委托
    keyboardContainer.addEventListener('click', (e) => {
        const keyBtn = e.target.closest('.key');
        if (!keyBtn) return;

        // 特殊功能：切换数字/符号
        if (keyBtn.dataset.action === 'toggle-symbol') {
            isSymbolMode = !isSymbolMode;
            btnToggleSymbol.textContent = isSymbolMode ? '数' : '符';
            renderNumRow();
            return;
        }

        const key = keyBtn.dataset.key;
        if (!key) return;

        // 处理修饰键
        if (keyBtn.classList.contains('key-modifier')) {
            if (activeModifiers.has(key)) {
                activeModifiers.delete(key);
                send({ keyup: key });
                keyBtn.classList.remove('active');
            } else {
                activeModifiers.add(key);
                send({ keydown: key });
                keyBtn.classList.add('active');
            }
            return;
        }

        // 普通按键
        send({ keydown: key });
        
        const releaseDelay = activeModifiers.size > 0 ? 50 : 20;
        setTimeout(() => {
            send({ keyup: key });
            if (activeModifiers.size > 0) clearModifiers();
        }, releaseDelay);
    });
});