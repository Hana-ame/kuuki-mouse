# 姿态解算核心数学 (纯 Python, 零依赖, 从 ~/my-node-app/lib/attitude.js 逐字移植)
#
# 坐标系约定 (与 W3C Device Orientation and Motion 规范一致, 与 my-node-app 同款):
#   - 设备系: x=屏幕右, y=屏幕顶, z=垂直于屏幕向外(屏幕法线/拍面)
#   - 地球系: X=东, Y=北, Z=天(上), 重力方向 = -Z
#   - alpha/beta/gamma 为 Z - X' - Y'' 内旋 Tait-Bryan 角:
#     先绕 z 转 alpha, 再绕新 x 转 beta, 再绕新 y 转 gamma
#   - quaternion q 表示 设备系 -> 地球系 (body -> world) 的旋转
#   - 静止时加速度计读数(含重力)指向"上", 即世界 +Z 在设备系中的方向
#
# 所有向量/四元数用 dict {'x','y','z'[, 'w']} 表示, 与 JS 版对象一一对应。
#
# 快速上手:
#   q = quat_from_device_euler(alpha, beta, gamma)   # 欧拉角 -> 四元数
#   f = q_rotate(q, {'x':0,'y':0,'z':1})             # 屏幕法线在世界系的方向
#   mah = Mahony(kp=0.5, ki=0.05)                    # Mahony 融合 (陀螺+重力)
#   mah.update(dt, gyro_rad_s, accel_ms2)

import math

DEG = math.pi / 180


# 角度归一化到 [-180, 180)
def wrap_deg(a):
    return ((a % 360) + 540) % 360 - 180


# ---------- 向量 ----------
def vec3(x, y, z):
    return {'x': x, 'y': y, 'z': z}


def normalize3(v):
    l = math.hypot(v['x'], v['y'], v['z']) or 1.0
    return {'x': v['x'] / l, 'y': v['y'] / l, 'z': v['z'] / l}


def cross(a, b):
    return {
        'x': a['y'] * b['z'] - a['z'] * b['y'],
        'y': a['z'] * b['x'] - a['x'] * b['z'],
        'z': a['x'] * b['y'] - a['y'] * b['x'],
    }


def dot(a, b):
    return a['x'] * b['x'] + a['y'] * b['y'] + a['z'] * b['z']


# ---------- 四元数 ----------
def q_identity():
    return {'x': 0, 'y': 0, 'z': 0, 'w': 1}


def q_normalize(q):
    l = math.sqrt(q['x'] * q['x'] + q['y'] * q['y'] + q['z'] * q['z'] + q['w'] * q['w']) or 1.0
    return {'x': q['x'] / l, 'y': q['y'] / l, 'z': q['z'] / l, 'w': q['w'] / l}


# 右乘 q ⊗ p
def q_mul(a, b):
    return {
        'w': a['w'] * b['w'] - a['x'] * b['x'] - a['y'] * b['y'] - a['z'] * b['z'],
        'x': a['w'] * b['x'] + a['x'] * b['w'] + a['y'] * b['z'] - a['z'] * b['y'],
        'y': a['w'] * b['y'] - a['x'] * b['z'] + a['y'] * b['w'] + a['z'] * b['x'],
        'z': a['w'] * b['z'] + a['x'] * b['y'] - a['y'] * b['x'] + a['z'] * b['w'],
    }


def q_conjugate(q):
    return {'x': -q['x'], 'y': -q['y'], 'z': -q['z'], 'w': q['w']}


def q_invert(q):
    n = q['x'] * q['x'] + q['y'] * q['y'] + q['z'] * q['z'] + q['w'] * q['w'] or 1.0
    c = q_conjugate(q)
    return {'x': c['x'] / n, 'y': c['y'] / n, 'z': c['z'] / n, 'w': c['w'] / n}


# 用四元数旋转向量 (body -> world)
def q_rotate(q, v):
    r = q_mul(q_mul(q, {'x': v['x'], 'y': v['y'], 'z': v['z'], 'w': 0}), q_conjugate(q))
    return {'x': r['x'], 'y': r['y'], 'z': r['z']}


# ---------- 旋转矩阵 (row-major, 9 元列表) ----------
# 标准 四元数->旋转矩阵 (设备系->地球系)
def mat_from_quat(q):
    x, y, z, w = q['x'], q['y'], q['z'], q['w']
    x2 = x + x; y2 = y + y; z2 = z + z
    xx = x * x2; xy = x * y2; xz = x * z2
    yy = y * y2; yz = y * z2; zz = z * z2
    wx = w * x2; wy = w * y2; wz = w * z2
    return [
        1 - (yy + zz), xy - wz, xz + wy,
        xy + wz, 1 - (xx + zz), yz - wx,
        xz - wy, yz + wx, 1 - (xx + yy),
    ]


# 旋转矩阵 -> 四元数 (Shoemake)
def quat_from_mat(m):
    t = m[0] + m[4] + m[8]
    if t > 0:
        s = math.sqrt(t + 1) * 2
        q = {'w': 0.25 * s, 'x': (m[7] - m[5]) / s, 'y': (m[2] - m[6]) / s, 'z': (m[3] - m[1]) / s}
    elif m[0] > m[4] and m[0] > m[8]:
        s = math.sqrt(1 + m[0] - m[4] - m[8]) * 2
        q = {'w': (m[7] - m[5]) / s, 'x': 0.25 * s, 'y': (m[1] + m[3]) / s, 'z': (m[2] + m[6]) / s}
    elif m[4] > m[8]:
        s = math.sqrt(1 + m[4] - m[0] - m[8]) * 2
        q = {'w': (m[2] - m[6]) / s, 'x': (m[1] + m[3]) / s, 'y': 0.25 * s, 'z': (m[5] + m[7]) / s}
    else:
        s = math.sqrt(1 + m[8] - m[0] - m[4]) * 2
        q = {'w': (m[3] - m[1]) / s, 'x': (m[2] + m[6]) / s, 'y': (m[5] + m[7]) / s, 'z': 0.25 * s}
    return q_normalize(q)


def mat_mul3(a, b):
    o = []
    for r in range(3):
        for c in range(3):
            o.append(a[r * 3] * b[c] + a[r * 3 + 1] * b[3 + c] + a[r * 3 + 2] * b[6 + c])
    return o


def mat_transpose(m):
    return [m[0], m[3], m[6], m[1], m[4], m[7], m[2], m[5], m[8]]


# W3C Z - X' - Y'' 内旋角 -> 设备系->地球系 旋转矩阵
def w3c_matrix(alpha_deg, beta_deg, gamma_deg):
    ca = math.cos(alpha_deg * DEG); sa = math.sin(alpha_deg * DEG)
    cb = math.cos(beta_deg * DEG); sb = math.sin(beta_deg * DEG)
    cg = math.cos(gamma_deg * DEG); sg = math.sin(gamma_deg * DEG)
    return [
        ca * cg - sa * sb * sg, -sa * cb, ca * sg + sa * sb * cg,
        sa * cg + ca * sb * sg, ca * cb, sa * sg - ca * sb * cg,
        -cb * sg, sb, cb * cg,
    ]


# W3C 角 -> 四元数 (设备系->地球系)
def quat_from_device_euler(alpha_deg, beta_deg, gamma_deg):
    return quat_from_mat(w3c_matrix(alpha_deg, beta_deg, gamma_deg))


# 四元数(设备系->地球系) -> W3C 角, 与设备自身报出的 alpha/beta/gamma 可比
def euler_from_quat(q):
    m = mat_from_quat(q)
    beta = math.asin(max(-1, min(1, m[7]))) / DEG
    alpha = math.atan2(-m[1], m[4]) / DEG
    gamma = math.atan2(-m[6], m[8]) / DEG
    alpha = (alpha % 360 + 360) % 360
    gamma = (gamma % 360 + 540) % 360 - 180
    return {'alpha': alpha, 'beta': beta, 'gamma': gamma}


# 世界 +Z(上) 方向在设备系中的表示 (静止加速度计读数归一化后应等于它)
def up_in_device(q):
    up = {'x': 0, 'y': 0, 'z': 1}
    inv = q_invert(q)
    return q_rotate(inv, up)


# 最短弧旋转: 从 from 单位向量转到 to 单位向量的四元数
def shortest_arc(from_v, to_v):
    f = normalize3(from_v); t = normalize3(to_v)
    d = f['x'] * t['x'] + f['y'] * t['y'] + f['z'] * t['z']
    if d > 1 - 1e-10:
        return {'x': 0, 'y': 0, 'z': 0, 'w': 1}
    if d < -1 + 1e-10:
        ax = {'x': 1, 'y': 0, 'z': 0} if abs(f['x']) < 0.9 else {'x': 0, 'y': 1, 'z': 0}
        c = normalize3({
            'x': f['y'] * ax['z'] - f['z'] * ax['y'],
            'y': f['z'] * ax['x'] - f['x'] * ax['z'],
            'z': f['x'] * ax['y'] - f['y'] * ax['x'],
        })
        return {'x': c['x'], 'y': c['y'], 'z': c['z'], 'w': 0}
    s = math.sqrt((1 + d) * 2)
    v = {
        'x': (f['y'] * t['z'] - f['z'] * t['y']) / s,
        'y': (f['z'] * t['x'] - f['x'] * t['z']) / s,
        'z': (f['x'] * t['y'] - f['y'] * t['x']) / s,
    }
    return q_normalize({'x': v['x'], 'y': v['y'], 'z': v['z'], 'w': s / 2})


# ---------- 仅重力(静态)解算 ----------
# 静止/慢速时由加速度计读数直接估计俯仰角 beta 与横滚角 gamma; 偏航 alpha 不可观测(需罗盘)。
# 原理: 加速度计(含重力)测的是"上"方向, 先由最短弧转到世界+Z, 解出 beta/gamma。
def accel_static_euler(accel, alpha_hint=0):
    a = normalize3(accel)
    u = {'x': -a['x'], 'y': -a['y'], 'z': -a['z']}   # 世界"上"在设备系中的方向
    e = euler_from_quat(shortest_arc(u, {'x': 0, 'y': 0, 'z': 1}))
    return {'alpha': alpha_hint, 'beta': e['beta'], 'gamma': e['gamma']}


def quat_from_accel(accel, alpha_hint=0):
    e = accel_static_euler(accel, alpha_hint)
    return quat_from_device_euler(alpha_hint, e['beta'], e['gamma'])


# ---------- Mahony 互补滤波 (陀螺仪积分 + 加速度计重力修正) ----------
# q 为 设备系->地球系 (body->world)。与 lib/attitude.js 的自研实现逐字一致。
class Mahony:
    def __init__(self, kp=0.5, ki=0.0):
        self.q = q_identity()
        self.e_int = {'x': 0, 'y': 0, 'z': 0}
        self.yaw_int = 0.0
        self.kp = kp
        self.ki = ki

    def reset(self):
        self.q = q_identity()
        self.e_int = {'x': 0, 'y': 0, 'z': 0}
        self.yaw_int = 0.0

    def set_orientation(self, q):
        self.q = q_normalize(q)
        self.e_int = {'x': 0, 'y': 0, 'z': 0}
        self.yaw_int = 0.0

    # gyro: 角速度 rad/s {x,y,z}(设备系, 右手规则); accel: 加速度 m/s² {x,y,z}(含重力)
    def update(self, dt, gyro, accel):
        ax, ay, az = accel['x'], accel['y'], accel['z']
        an = math.hypot(ax, ay, az)
        if an > 0.1:
            ax /= an; ay /= an; az /= an
        else:
            ax = 0; ay = 0; az = 0

        x, y, z, w = self.q['x'], self.q['y'], self.q['z'], self.q['w']

        # 世界 +Z(上) 在设备系中的估计方向
        vx = 2 * (x * z - w * y)
        vy = 2 * (w * x + y * z)
        vz = w * w - x * x - y * y + z * z

        # 误差 = a × v (测量上方向 × 估计上方向)
        ex = ay * vz - az * vy
        ey = az * vx - ax * vz
        ez = ax * vy - ay * vx

        kp, ki = self.kp, self.ki
        self.e_int['x'] += ex * ki * dt
        self.e_int['y'] += ey * ki * dt
        self.e_int['z'] += ez * ki * dt

        gx = gyro['x'] + kp * ex + self.e_int['x']
        gy = gyro['y'] + kp * ey + self.e_int['y']
        gz = gyro['z'] + kp * ez + self.e_int['z']

        # q̇ = ½ q ⊗ ω (ω 为设备系角速度)
        dq = {
            'x': 0.5 * dt * (w * gx + y * gz - z * gy),
            'y': 0.5 * dt * (w * gy - x * gz + z * gx),
            'z': 0.5 * dt * (w * gz + x * gy - y * gx),
            'w': 0.5 * dt * (-x * gx - y * gy - z * gz),
        }
        self.q = q_normalize({
            'x': x + dq['x'], 'y': y + dq['y'], 'z': z + dq['z'], 'w': w + dq['w'],
        })
        return self.q

    # 连续北向校准: 用罗盘绝对航向 alphaDeg (来自 deviceorientationabsolute, W3C 0=北)
    # 修正融合解绕世界 Z 轴的偏航漂移。kp 决定跟随速度, ki 积分消除静态偏航零偏。
    def correct_yaw(self, alpha_deg, kp=0.3, ki=0.02):
        e = euler_from_quat(self.q)
        alpha, beta = e['alpha'], e['beta']
        # 距竖立(β=±90)的角度; 90=水平, 0=完全竖立(万向锁)。过近则横摇/偏航不可分, 应减弱。
        verticalness = abs(math.cos(beta * DEG))  # 竖立≈1, 水平≈0
        if verticalness < 0.12:
            return 0.0
        scale = max(0.0, min(1.0, (1 - verticalness) / 0.88))
        raw = wrap_deg(alpha_deg - alpha)
        clamp = 45 / (0.2 + kp)       # 单帧偏航修正角上限, 堵住 |err|≈180 的一次性跳变
        err = max(-clamp, min(clamp, raw)) * scale
        self.yaw_int += err * ki
        th = (err * kp + self.yaw_int) * DEG
        h = th / 2
        qz = {'x': 0, 'y': 0, 'z': math.sin(h), 'w': math.cos(h)}
        self.q = q_normalize(q_mul(qz, self.q))
        return err


# 纯陀螺仪积分 (演示漂移)
class GyroIntegrator:
    def __init__(self):
        self.q = q_identity()

    def reset(self):
        self.q = q_identity()

    def set_orientation(self, q):
        self.q = q_normalize(q)

    def update(self, dt, gyro):
        x, y, z, w = self.q['x'], self.q['y'], self.q['z'], self.q['w']
        dq = {
            'x': 0.5 * dt * (w * gyro['x'] + y * gyro['z'] - z * gyro['y']),
            'y': 0.5 * dt * (w * gyro['y'] - x * gyro['z'] + z * gyro['x']),
            'z': 0.5 * dt * (w * gyro['z'] + x * gyro['y'] - y * gyro['x']),
            'w': 0.5 * dt * (-x * gyro['x'] - y * gyro['y'] - z * gyro['z']),
        }
        self.q = q_normalize({
            'x': x + dq['x'], 'y': y + dq['y'], 'z': z + dq['z'], 'w': w + dq['w'],
        })
        return self.q


# ---------- 指向/前向分解 (移植自 ~/my-node-app/src/motion/forward.js) ----------
# 世界系: z 向上, 重力 down = (0,0,-1); 设备系 x=右, y=顶, z=屏幕法线(拍面)
# 屏幕法线 = qRotate(q, {0,0,1}) 即"正前方"方向。
def compute_forward(beta_deg, gamma_deg):
    q = quat_from_device_euler(0, beta_deg, gamma_deg)
    forward = normalize3(q_rotate(q, {'x': 0, 'y': 0, 'z': 1}))

    # 手机顶边(长轴)方向在世界系: 只随 beta(重力倾斜)变化, 与 gamma 无关
    top = q_rotate(q, {'x': 0, 'y': 1, 'z': 0})

    return {
        'forward': forward,
        'top': top,
        'beta': beta_deg * DEG,
        'gamma': gamma_deg * DEG,
        'elevation': math.asin(max(-1, min(1, forward['z']))),
        'azimuth': math.atan2(forward['x'], forward['y']),
    }


# 相对游戏屏平面分解
# ref_dir: 校准时「正前」对应的 forward(指向屏幕中心), 定义屏幕平面的法线
def screen_rel(dir_v, ref_dir):
    n = normalize3(ref_dir)
    up = {'x': 0, 'y': 0, 'z': 1}
    right = normalize3(cross(n, up))
    vert = cross(right, n)

    dp = dot(dir_v, n)
    angle_to_plane = math.asin(max(-1, min(1, dp)))

    in_plane = {
        'x': dir_v['x'] - dp * n['x'],
        'y': dir_v['y'] - dp * n['y'],
        'z': dir_v['z'] - dp * n['z'],
    }
    return {
        'angle_to_plane': angle_to_plane,
        'on_screen': {'x': dot(in_plane, right), 'y': dot(in_plane, vert)},
    }
