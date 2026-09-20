# 姿态解算数学验证 (纯 Python): W3C 矩阵 / 四元数往返 / Mahony 融合跟踪
# 移植自 ~/my-node-app/verify-attitude.mjs (与 lib/attitude.js 自研实现同款算法)
import math
import random

# 结果里的 "[PASS]" 和 "全部通过" 是中文, 英文 Windows (cp1252) 上 print 会
# UnicodeEncodeError —— 这个脚本在英文 CI 上崩过一次。见 utf8_stdio.py
from utf8_stdio import force_utf8_stdio

force_utf8_stdio()

# 固定随机种子: 仿真含陀螺/加速度计噪声, 种子保证可复现(避免 Mahony vs 纯陀螺漂移对比的边界抖动)
random.seed(42)

from attitude import (
    DEG, wrap_deg, w3c_matrix, quat_from_device_euler, euler_from_quat,
    mat_from_quat, mat_mul3, mat_transpose, q_rotate, q_invert, q_mul,
    up_in_device, Mahony, GyroIntegrator, quat_from_accel, accel_static_euler,
)

fail = 0


def ok(label, cond, detail=''):
    global fail
    if not cond:
        fail += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


def near(a, b, tol=1e-6):
    return abs(a - b) <= tol


def quat_err(q, gt):
    d = q_mul(q_invert(q), gt)
    return 2 * math.acos(max(-1, min(1, abs(d['w'])))) / DEG


def tilt_err(q, q0):
    u = up_in_device(q)
    u0 = up_in_device(q0)
    cross_v = math.hypot(
        u['y'] * u0['z'] - u['z'] * u0['y'],
        u['z'] * u0['x'] - u['x'] * u0['z'],
        u['x'] * u0['y'] - u['y'] * u0['x'],
    )
    return math.asin(max(-1, min(1, cross_v))) / DEG


def simulate_trajectory():
    t0, t1, dt = 0, 20, 1 / 60
    N = round((t1 - t0) / dt)
    angles = []
    gyros = []
    accels = []
    q_prev = None
    for i in range(N):
        t = i * dt
        alpha = 40 * math.sin(0.4 * t) + 90
        beta = 45 * math.sin(0.9 * t) + 45
        gamma = 30 * math.sin(1.3 * t)
        q = quat_from_device_euler(alpha, beta, gamma)
        angles.append({'alpha': alpha, 'beta': beta, 'gamma': gamma, 'q': q})
        if q_prev:
            # 数值微分得到设备系角速度: ω = 2·log(q̄⁻¹⊗q)/dt
            dq = q_mul(q_invert(q_prev), q)
            ang = 2 * math.acos(max(-1, min(1, dq['w'])))
            n = math.hypot(dq['x'], dq['y'], dq['z'])
            axis = {'x': dq['x'] / n, 'y': dq['y'] / n, 'z': dq['z'] / n} if n > 1e-9 else {'x': 0, 'y': 0, 'z': 1}
            mag = ang / dt if abs(ang) > 1e-9 else 0
            gyros.append({
                'x': axis['x'] * mag + (random.random() - 0.5) * 0.02,
                'y': axis['y'] * mag + (random.random() - 0.5) * 0.02,
                'z': axis['z'] * mag + (random.random() - 0.5) * 0.02,
            })
        else:
            gyros.append({'x': 0, 'y': 0, 'z': 0})
        u = up_in_device(q)
        accels.append({
            'x': u['x'] * 9.8 + (random.random() - 0.5) * 0.3,
            'y': u['y'] * 9.8 + (random.random() - 0.5) * 0.3,
            'z': u['z'] * 9.8 + (random.random() - 0.5) * 0.3,
        })
        q_prev = q
    return {'angles': angles, 'gyros': gyros, 'accels': accels, 'dt': dt}


print('== W3C 矩阵 = 标准旋转(正交, det=+1) ==')
ortho_ok = True
for _ in range(200):
    a = random.random() * 360
    b = random.random() * 170 - 85
    g = random.random() * 170 - 85
    m = w3c_matrix(a, b, g)
    mt = mat_transpose(m)
    I = mat_mul3(m, mt)
    for k in range(9):
        want = 1 if k % 4 == 0 else 0
        if not near(I[k], want, 1e-9):
            ortho_ok = False
            break
    if not ortho_ok:
        break
ok('200 组随机角正交性 M·Mᵀ=I', ortho_ok)

print('== 四元数 <-> W3C 角 往返 ==')
bad = 0
for _ in range(200):
    a = random.random() * 360
    b = random.random() * 160 - 80
    g = random.random() * 160 - 80
    q = quat_from_device_euler(a, b, g)
    e = euler_from_quat(q)
    q2 = quat_from_device_euler(e['alpha'], e['beta'], e['gamma'])
    d = (abs(q['w'] - q2['w']) + abs(q['x'] - q2['x'])
         + abs(q['y'] - q2['y']) + abs(q['z'] - q2['z']))
    if d > 1e-6:
        bad += 1
ok('200 组随机角 角->四元数->角 误差 < 1e-6', bad == 0, f'失败 {bad}')

print('== W3C 规范示例 ==')
# 例1: 平放桌面, 屏幕朝上, 顶边朝西 -> alpha=90,beta=0,gamma=0
q = quat_from_device_euler(90, 0, 0)
top = q_rotate(q, {'x': 0, 'y': 1, 'z': 0})
out = q_rotate(q, {'x': 0, 'y': 0, 'z': 1})
u = up_in_device(q)
ok('平放顶朝西: 顶边->西(-X)', near(top['x'], -1, 1e-9) and near(top['y'], 0, 1e-9), f"({top['x']:.3f},{top['y']:.3f},{top['z']:.3f})")
ok('平放顶朝西: 屏幕法线->天(+Z)', near(out['z'], 1, 1e-9) and near(out['x'], 0, 1e-9), f"({out['x']:.3f},{out['y']:.3f},{out['z']:.3f})")
ok('平放顶朝西: 上方向=(0,0,1) (加速度计读数)', near(u['x'], 0, 1e-9) and near(u['y'], 0, 1e-9) and near(u['z'], 1, 1e-9), f"({u['x']:.3f},{u['y']:.3f},{u['z']:.3f})")
# 例2: 屏幕竖直, 顶边朝上 -> beta=90 (与 alpha,gamma 无关)
q = quat_from_device_euler(0, 90, 0)
top = q_rotate(q, {'x': 0, 'y': 1, 'z': 0})
out = q_rotate(q, {'x': 0, 'y': 0, 'z': 1})
u = up_in_device(q)
ok('竖立顶朝上: 顶边->天(+Z)', near(top['z'], 1, 1e-9) and near(top['x'], 0, 1e-9) and near(top['y'], 0, 1e-9), f"({top['x']:.3f},{top['y']:.3f},{top['z']:.3f})")
ok('竖立顶朝上(alpha=0): 屏幕法线->南(-Y)', near(out['y'], -1, 1e-9) and near(out['x'], 0, 1e-9) and near(out['z'], 0, 1e-9), f"({out['x']:.3f},{out['y']:.3f},{out['z']:.3f})")
ok('竖立顶朝上: 上方向=(0,1,0) (加速度计读数)', near(u['x'], 0, 1e-9) and near(u['y'], 1, 1e-9) and near(u['z'], 0, 1e-9), f"({u['x']:.3f},{u['y']:.3f},{u['z']:.3f})")
# 例3: upInDevice = Mᵀ·(0,0,1)
q = quat_from_device_euler(30, 45, 20)
u = up_in_device(q)
m = mat_from_quat(q)
ok('upInDevice = Mᵀ·(0,0,1)', near(u['x'], m[6], 1e-9) and near(u['y'], m[7], 1e-9) and near(u['z'], m[8], 1e-9), f"u=({u['x']:.3f},{u['y']:.3f},{u['z']:.3f})")

print('== 仅重力(静态)解算 ==')
e = accel_static_euler({'x': 0, 'y': 0, 'z': -9.8}, 0)
ok('平放屏上(0,0,-9.8) -> beta=0,gamma=0', near(e['beta'], 0, 1e-9) and near(e['gamma'], 0, 1e-9), f"β={e['beta']:.2f} γ={e['gamma']:.2f}")
e1b = accel_static_euler({'x': 0, 'y': 0, 'z': 9.8}, 0)
ok('平放屏下(0,0,9.8) -> beta=0,gamma=±180', near(e1b['beta'], 0, 1e-9) and near(abs(e1b['gamma']), 180, 1e-6), f"β={e1b['beta']:.2f} γ={e1b['gamma']:.2f}")
e2 = accel_static_euler({'x': 0, 'y': -9.8, 'z': 0}, 0)
ok('竖立顶朝上(0,-9.8,0) -> beta=90', near(e2['beta'], 90, 1e-6), f"β={e2['beta']:.2f}")
e3 = accel_static_euler({
    'x': 9.8 * math.cos(45 * DEG) * math.sin(30 * DEG),
    'y': -9.8 * math.sin(45 * DEG),
    'z': -9.8 * math.cos(45 * DEG) * math.cos(30 * DEG),
}, 0)
ok('beta=45,gamma=30 读回一致', near(e3['beta'], 45, 1e-6) and near(e3['gamma'], 30, 1e-6), f"β={e3['beta']:.2f} γ={e3['gamma']:.2f}")

print('== Mahony 融合跟踪 (固定增益, 陀螺噪声 + 恒定零偏, 20s) ==')
sim = simulate_trajectory()
angles, gyros, accels, dt = sim['angles'], sim['gyros'], sim['accels'], sim['dt']
mah = Mahony(kp=0.5, ki=0.05)
gyro_int = GyroIntegrator()
mah.set_orientation(angles[0]['q'])
gyro_int.set_orientation(angles[0]['q'])
max_mah, max_gyro = 0, 0
warmup = 1.0
for i in range(1, len(angles)):
    t = i * dt
    bias = {'x': 0.2 * DEG, 'y': -0.15 * DEG, 'z': 0.1 * DEG}
    g = {'x': gyros[i]['x'] + bias['x'], 'y': gyros[i]['y'] + bias['y'], 'z': gyros[i]['z'] + bias['z']}
    mah.update(dt, g, accels[i])
    gyro_int.update(dt, g)
    if t < warmup:
        continue
    max_mah = max(max_mah, quat_err(mah.q, angles[i]['q']))
    max_gyro = max(max_gyro, quat_err(gyro_int.q, angles[i]['q']))
ok(f'Mahony 融合(固定增益+零偏) 最大姿态误差 < 3°', max_mah < 3, f'max={max_mah:.2f}°')
ok(f'纯陀螺积分(带零偏) 明显漂移(> Mahony + 3°)', max_gyro > max_mah + 3, f'max={max_gyro:.2f}°')

print('== 陀螺零偏抑制 (Ki>0 的积分项, 静态场景) ==')
dt = 1 / 60
N = 20 * 60
q0 = quat_from_device_euler(60, 40, -20)
up0 = up_in_device(q0)
accel = {'x': up0['x'] * 9.8, 'y': up0['y'] * 9.8, 'z': up0['z'] * 9.8}
bias = {'x': 0.5 * DEG, 'y': -0.3 * DEG, 'z': 0.2 * DEG}
mah = Mahony(kp=0.8, ki=0.2)
gyro_int = GyroIntegrator()
mah.set_orientation(q0)
gyro_int.set_orientation(q0)
for _ in range(1, N + 1):
    mah.update(dt, bias, accel)
    gyro_int.update(dt, bias)
final_mah = tilt_err(mah.q, q0)
final_gyro = tilt_err(gyro_int.q, q0)
ok('带零偏陀螺静止 20s: Mahony 上方向倾角误差 < 0.1° (积分项消零偏)', final_mah < 0.1, f'err={final_mah:.3f}°')
ok('带零偏陀螺静止 20s: 纯陀螺积分倾角漂移明显(> 1°)', final_gyro > 1, f'err={final_gyro:.2f}°')

print('== wrapDeg 归一化 (映射到 [-180,180)) ==')
ok('wrapDeg(370)=10', near(wrap_deg(370), 10))
ok('wrapDeg(-10)=-10 ≡ 350', near(wrap_deg(-10), -10) or near(wrap_deg(-10), 350))
ok('wrapDeg(200)=-160', near(wrap_deg(200), -160))
ok('wrapDeg(-200)=160', near(wrap_deg(-200), 160))
ok('wrapDeg(180)=180 或 -180', near(abs(wrap_deg(180)), 180))

print()
print('全部通过' if fail == 0 else f'{fail} 项失败')
raise SystemExit(0 if fail == 0 else 1)
