#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from typing import Dict, Any, Tuple, List

POSITION_EPSILON = 1e-9


def validate_strategy_config(cfg: Dict[str, Any], name: str = 'strategy') -> None:
    """校验单个标的参数，提前拦截 0、负数、权重异常、目标区间倒置等配置问题。"""
    def positive(key: str, default: Any = None) -> float:
        value = cfg.get(key, default)
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError(f'{name}: {key} must be a number, got {value!r}')
        if value <= 0:
            raise ValueError(f'{name}: {key} must be > 0, got {value}')
        return value

    base_units = float(cfg.get('base_units', 0) or 0)
    if base_units < 0:
        raise ValueError(f'{name}: base_units must be >= 0, got {base_units}')
    target_units = positive('target_units', 1.0)
    base_units = positive('base_units', target_units)
    limit_target = positive('limit_target', 2.0)
    if limit_target < 1:
        raise ValueError(f'{name}: limit_target must be >= 1, got {limit_target}')
    if base_units * limit_target < target_units:
        raise ValueError(f'{name}: base_units * limit_target must be >= target_units; got {base_units} * {limit_target} < {target_units}')

    trend_multiple = positive('trend_multiple', 1.2)
    sell_multiple = positive('sell_multiple', 1.5)
    if trend_multiple >= sell_multiple:
        raise ValueError(f'{name}: trend_multiple must be < sell_multiple')

    positive('trend_zone_step_percent', 0.01)
    positive('trend_zone_sell_percent', 0.05)
    positive('clear_zone_step_percent', 0.04)
    positive('pyramid_add_step', cfg.get('add_box_step', 0.04))
    positive('box_add_step', cfg.get('add_box_step', 0.04))
    positive('add_box_units_percent', 0.1)

    # 校验 pyramid_sell_flag（可选，默认为 off）
    sell_flag = cfg.get('pyramid_sell_flag', 'off')
    if sell_flag not in ('on', 'off'):
        raise ValueError(f'{name}: pyramid_sell_flag must be "on" or "off", got {sell_flag!r}')

    weights = cfg.get('pyramid_add_weights', cfg.get('pyramid_weights'))
    if weights is not None:
        if not isinstance(weights, list) or not weights:
            raise ValueError(f'{name}: pyramid_add_weights must be a non-empty list')
        weights = [float(w) for w in weights]
        if any(w <= 0 for w in weights):
            raise ValueError(f'{name}: pyramid_add_weights must all be > 0')
        if abs(sum(weights) - 1.0) > 1e-6:
            raise ValueError(f'{name}: pyramid_add_weights must sum to 1.0')
        steps = int(cfg.get('pyramid_add_steps', cfg.get('pyramid_steps', len(weights))))
        if steps <= 0 or steps > len(weights):
            raise ValueError(f'{name}: pyramid_add_steps must be in [1, len(weights)]')


def validate_all_strategy_configs(config: Dict[str, Any]) -> None:
    """批量校验整份配置，建议在读取 YAML 后、正式运行策略前调用。"""
    for name, cfg in config.items():
        if isinstance(cfg, dict):
            validate_strategy_config(cfg, name=str(name))


def get_zone(price: float, ma150: float, cfg: Dict[str, Any]) -> str:
    """根据价格与 MA150 的倍数关系，返回 CHANCE/BOX/TREND/CLEAR 四个区域之一。"""
    if ma150 is None or ma150 <= 0:
        return 'BOX_ZONE'
    trend_multiple = float(cfg.get('trend_multiple', 1.2))
    sell_multiple = float(cfg.get('sell_multiple', 1.5))
    if price < ma150:
        return 'CHANCE_ZONE'
    if ma150 <= price < ma150 * trend_multiple:
        return 'BOX_ZONE'
    if ma150 * trend_multiple <= price < ma150 * sell_multiple:
        return 'TREND_ZONE'
    return 'CLEAR_ZONE'


def normalize_position_amount(value: float, mode: str, lot_size: int = 100) -> float:
    """规范化仓位数量：percent 模式保留小数，非 percent 模式按整手 lot_size 处理。"""
    value = max(value, 0.0)
    if mode == 'percent':
        return round(value, 6)
    if value <= POSITION_EPSILON:
        return 0.0
    rounded = int(value // lot_size) * lot_size
    if rounded == 0 and value > 0:
        rounded = lot_size
    return float(rounded)


def calculate_pyramid_sell_plan(target_units: float, pyramid_weights: List[float], mode: str, lot_size: int = 100) -> List[Dict[str, Any]]:
    """按权重生成分档卖出计划，最后一档自动处理剩余数量。"""
    total = max(target_units, 0.0)
    if total <= POSITION_EPSILON:
        return []
    plan = []
    for i, w in enumerate(pyramid_weights):
        units = normalize_position_amount(total * w, mode, lot_size)
        if i == len(pyramid_weights) - 1:
            allocated = sum(p['units'] for p in plan)
            remaining = total - allocated
            units = normalize_position_amount(remaining, mode, lot_size) if remaining > POSITION_EPSILON else 0.0
        if units > POSITION_EPSILON:
            plan.append({'step': i + 1, 'units': units, 'weight_percent': round(w * 100, 1)})
    return plan


def get_pyramid_sell_target_step(price: float, ma150: float, cfg: Dict[str, Any], total_steps: int) -> int:
    """价格进入 CLEAR_ZONE 后，按 clear_zone_step_percent 计算应卖到第几档。"""
    if ma150 is None or ma150 <= 0 or total_steps <= 0:
        return 0
    sell_multiple = float(cfg.get('sell_multiple', 1.5))
    current_multiple = price / ma150
    if current_multiple < sell_multiple:
        return 0
    step_pct = float(cfg.get('clear_zone_step_percent', 0.04))
    if step_pct <= 0:
        raise ValueError('clear_zone_step_percent must be > 0')
    step = int((current_multiple - sell_multiple) / step_pct) + 1
    return min(max(step, 0), total_steps)


def get_trend_sell_decision(state: Dict[str, Any], cfg: Dict[str, Any], base_units: float, mode: str, current_price: float, ma150: float, lot_size: int = 100) -> Tuple[float, Dict[str, Any]]:
    """趋势区阶梯卖出；只卖出高于 base_units 的机动仓，首次进入趋势区只记录锚点。"""
    new_state = state.copy()
    last_trade_price = state.get('last_trade_price', 0.0)
    if last_trade_price <= 0:
        new_state['last_trade_price'] = current_price
        return 0.0, new_state

    step_pct = float(cfg.get('trend_zone_step_percent', 0.01))
    sell_pct = float(cfg.get('trend_zone_sell_percent', 0.05))
    if step_pct <= 0:
        raise ValueError('trend_zone_step_percent must be > 0')
    if sell_pct <= 0:
        raise ValueError('trend_zone_sell_percent must be > 0')

    sell_qty = 0.0
    if current_price - last_trade_price >= last_trade_price * step_pct - POSITION_EPSILON:
        current_units = state.get('current_units', 0.0)
        excess = max(current_units - base_units, 0.0)
        if excess > POSITION_EPSILON:
            sell_qty = normalize_position_amount(min(excess, current_units * sell_pct), mode, lot_size)
            if sell_qty > POSITION_EPSILON:
                new_state['last_trade_price'] = current_price
    return sell_qty, new_state


def get_pyramid_add_enabled(cfg: Dict[str, Any]) -> str:
    """读取倒金字塔加仓开关；只有 yes 返回 yes，其他值统一视为 auto。"""
    value = str(cfg.get('pyramid_add_enabled', 'auto')).strip().lower()
    return 'yes' if value == 'yes' else 'auto'


def evaluate_pyramid_add_runtime(state: Dict[str, Any], cfg: Dict[str, Any], current_price: float, ma150: float, zone: str, target_units: float) -> Tuple[Dict[str, Any], Dict[str, Any], List[str], str]:
    """维护机会区倒金字塔运行状态。

    新周期规则：
    1. 首次进入 CHANCE_ZONE 时，以当时 MA150 锁定第0步锚点。
    2. 回到 BOX_ZONE 不重置；后续再次进入 CHANCE_ZONE 继续沿用旧步数与 last_add_price。
    3. 进入 TREND_ZONE/CLEAR_ZONE 表示本轮低吸周期结束，才重置机会区倒金字塔。
    """
    new_state = state.copy()
    cfg_updates: Dict[str, Any] = {}
    events: List[str] = []
    config_mode = get_pyramid_add_enabled(cfg)
    effective_mode = config_mode

    def reset_pyramid(reason: str = 'PYRAMID_SWITCH_TO_AUTO'):
        nonlocal effective_mode
        effective_mode = 'auto'
        cfg_updates['pyramid_add_enabled'] = 'auto'
        new_state['pyramid_add_active'] = False
        new_state['pyramid_step'] = 0
        new_state['target_reached_once'] = False
        new_state['pyramid_start_units'] = None
        new_state['pyramid_limit_units'] = None
        new_state['pyramid_anchor_price'] = None
        new_state['last_add_price'] = None
        events.append(reason)

    # 低吸周期在进入趋势/高估区后结束。BOX 只是反弹，不重置。
    if zone in {'TREND_ZONE', 'CLEAR_ZONE'}:
        if bool(state.get('pyramid_add_active', False)) or config_mode == 'yes':
            reset_pyramid('PYRAMID_RESET_TO_TREND')
        else:
            new_state['pyramid_add_active'] = False
        return new_state, cfg_updates, events, effective_mode

    active = bool(state.get('pyramid_add_active', False))

    # BOX 不主动启动倒金字塔；若此前 CHANCE 已激活，则在 BOX 中延续同一轮状态。
    if zone == 'BOX_ZONE':
        if active or config_mode == 'yes':
            effective_mode = 'yes'
            new_state['pyramid_add_active'] = True
        else:
            effective_mode = 'auto'
            new_state['pyramid_add_active'] = False
        return new_state, cfg_updates, events, effective_mode

    if zone != 'CHANCE_ZONE':
        return new_state, cfg_updates, events, effective_mode

    # 已激活的机会区周期再次进入 CHANCE 时，沿用旧锚点和旧步数。
    if active or config_mode == 'yes':
        effective_mode = 'yes'
        new_state['pyramid_add_active'] = True
        if not new_state.get('pyramid_anchor_price'):
            anchor_price = ma150 if ma150 is not None and ma150 > 0 else current_price
            new_state['pyramid_anchor_price'] = anchor_price
            new_state['last_add_price'] = new_state.get('last_add_price') or anchor_price
        return new_state, cfg_updates, events, effective_mode

    # auto 模式首次进入 CHANCE_ZONE 时自动开启，并用当时 MA150 锁定第0步。
    effective_mode = 'yes'
    cfg_updates['pyramid_add_enabled'] = 'yes'
    new_state['pyramid_add_active'] = True
    new_state['pyramid_step'] = 0
    new_state['target_reached_once'] = state.get('current_units', 0.0) >= target_units - POSITION_EPSILON
    anchor_price = ma150 if ma150 is not None and ma150 > 0 else current_price
    new_state['last_add_price'] = anchor_price
    new_state['pyramid_anchor_price'] = anchor_price
    new_state['pyramid_start_units'] = None
    new_state['pyramid_limit_units'] = None
    events.append('PYRAMID_AUTO_TRIGGERED')
    return new_state, cfg_updates, events, effective_mode


def evaluate_pyramid_sell_runtime(state: Dict[str, Any], cfg: Dict[str, Any], zone: str, current_price: float) -> Tuple[Dict[str, Any], Dict[str, Any], List[str], str]:
    """维护 Clear 区倒金字塔卖出运行状态。
    
    规则：
    1. pyramid_sell_flag 配置默认 off，作为手动总开关
    2. 首次进入 CLEAR_ZONE 时，自动将 pyramid_sell_flag 置为 on
    3. 价格跌回 BOX_ZONE 或 CHANCE_ZONE 时，重置并将 pyramid_sell_flag 置为 off
    4. TREND_ZONE 中不重置，保持原状态
    """
    new_state = state.copy()
    cfg_updates: Dict[str, Any] = {}
    events: List[str] = []
    
    # 读取配置中的手动开关（默认 off）
    manual_override = str(cfg.get('pyramid_sell_flag', 'off')).strip().lower()
    # 实际生效模式：优先使用运行时状态，否则使用手动配置
    runtime_active = new_state.get('pyramid_sell_active', False)
    
    # 确定有效模式：如果手动开关是 off，则完全关闭；否则由运行时状态决定
    if manual_override == 'off':
        effective_mode = 'off'
    else:
        effective_mode = 'on' if runtime_active else 'off'
    
    def reset_sell(reason: str):
        nonlocal effective_mode
        new_state['pyramid_sell_active'] = False
        new_state['pyramid_sell_step'] = 0
        new_state['pyramid_sell_anchor_price'] = None
        new_state['pyramid_sell_last_price'] = None
        # 重置时，如果手动开关不是强制 on，则将配置更新为 off
        if manual_override != 'on':
            cfg_updates['pyramid_sell_flag'] = 'off'
            effective_mode = 'off'
        events.append(reason)
    
    def activate_sell(reason: str):
        nonlocal effective_mode
        new_state['pyramid_sell_active'] = True
        new_state['pyramid_sell_step'] = 0
        new_state['pyramid_sell_anchor_price'] = current_price
        new_state['pyramid_sell_last_price'] = current_price
        # 自动将配置更新为 on
        cfg_updates['pyramid_sell_flag'] = 'on'
        effective_mode = 'on'
        events.append(reason)
    
    # 重置条件：BOX_ZONE 或 CHANCE_ZONE（跌回低位区域）
    if zone in {'BOX_ZONE', 'CHANCE_ZONE'}:
        if runtime_active:
            reset_sell('PYRAMID_SELL_RESET_BY_DROP')
        return new_state, cfg_updates, events, effective_mode
    
    # TREND_ZONE：不重置，保持原状态
    if zone == 'TREND_ZONE':
        return new_state, cfg_updates, events, effective_mode
    
    # CLEAR_ZONE 逻辑
    if zone != 'CLEAR_ZONE':
        return new_state, cfg_updates, events, effective_mode
    
    # 手动开关为 off 时，完全禁止
    if manual_override == 'off':
        if runtime_active:
            reset_sell('PYRAMID_SELL_MANUAL_OFF')
        return new_state, cfg_updates, events, 'off'
    
    # 首次进入 Clear 区：自动激活
    if not runtime_active:
        activate_sell('PYRAMID_SELL_AUTO_TRIGGERED')
    
    return new_state, cfg_updates, events, effective_mode


def _get_crossed_pyramid_step(anchor_price: float, current_price: float, step_pct: float, total_steps: int) -> int:
    """按 MA150 第0步锚点，计算当前价格已经跌破到第几档。"""
    if anchor_price is None or anchor_price <= 0 or current_price is None or current_price <= 0:
        return 0
    step_pct = float(step_pct)
    if step_pct <= 0 or total_steps <= 0:
        return 0
    crossed = 0
    trigger_price = anchor_price
    for i in range(1, total_steps + 1):
        trigger_price *= (1 - step_pct)
        if current_price <= trigger_price + POSITION_EPSILON:
            crossed = i
        else:
            break
    return crossed


def _get_pyramid_step_price(anchor_price: float, step_pct: float, step: int) -> float:
    """返回某一倒金字塔步数对应的理论锚点价格。"""
    if anchor_price is None or anchor_price <= 0:
        return 0.0
    step = max(0, int(step or 0))
    return float(anchor_price) * ((1 - float(step_pct)) ** step)


def _get_crossed_clear_step(anchor_price: float, current_price: float, step_pct: float, total_steps: int) -> int:
    """按 Clear 锚点计算当前价格已经上穿到第几档。
    
    第1档触发价 = anchor_price * (1 + step_pct)
    第2档触发价 = anchor_price * (1 + step_pct)^2
    以此类推（与机会区对称）
    """
    if anchor_price is None or anchor_price <= 0 or current_price is None or current_price <= 0:
        return 0
    step_pct = float(step_pct)
    if step_pct <= 0 or total_steps <= 0:
        return 0
    
    crossed = 0
    for i in range(1, total_steps + 1):
        trigger_price = anchor_price * ((1 + step_pct) ** i)
        if current_price >= trigger_price - POSITION_EPSILON:
            crossed = i
        else:
            break
    return crossed


def get_clear_pyramid_target_step(
    current_price: float, 
    state: Dict[str, Any], 
    cfg: Dict[str, Any], 
    total_steps: int
) -> int:
    """Clear区倒金字塔清底仓目标步数，使用状态中维护的锚点。"""
    step_pct = float(cfg.get('clear_zone_step_percent', 0.04))
    if step_pct <= 0:
        raise ValueError('clear_zone_step_percent must be > 0')
    
    anchor_price = state.get('pyramid_sell_anchor_price', 0.0)
    if anchor_price <= 0:
        return 0
    
    return min(max(_get_crossed_clear_step(anchor_price, current_price, step_pct, total_steps), 0), total_steps)


def get_clear_step_price(anchor_price: float, step_pct: float, step: int) -> float:
    """返回 Clear 区某一卖出步数对应的理论触发价格。
    
    第1档触发价 = anchor * (1 + step_pct)
    第2档触发价 = anchor * (1 + step_pct)^2
    """
    if anchor_price <= 0:
        return 0.0
    step = max(1, int(step or 1))
    return float(anchor_price) * ((1 + float(step_pct)) ** step)


def _format_step_reason(prefix: str, start_step: int, end_step: int) -> str:
    if end_step <= 0 or end_step < start_step:
        return prefix
    if start_step == end_step:
        return f'{prefix}_{end_step}'
    return f'{prefix}_{start_step}_TO_{end_step}'


def get_pyramid_add_decision(state: Dict[str, Any], cfg: Dict[str, Any], target_units: float, limit_units: float, current_price: float, ma150: float, mode: str, lot_size: int = 100) -> Tuple[float, Dict[str, Any], str]:
    """机会区倒金字塔加仓。

    规则：
    1. 以 MA150 作为第0步价格锚点，而不是以首次运行时的当前价作为锚点。
    2. 若进入机会区时当前仓位低于补仓初始仓位 target_units，先补到 target_units。
    3. 若当前仓位已达到或超过 target_units，则不补初始仓，直接以当前仓位作为本轮起点。
    4. 锁定 pyramid_start_units / pyramid_limit_units，后续每档预算不随 current_units 漂移。
    5. 中途添加标的时，按 MA150 到当前价已经跨过的档位一次性追认加仓步数。
    """
    pyramid_add_step = float(cfg.get('pyramid_add_step', cfg.get('add_box_step', 0.04)))
    if pyramid_add_step <= 0:
        raise ValueError('pyramid_add_step must be > 0')
    pyramid_weights = cfg.get('pyramid_add_weights', cfg.get('pyramid_weights', [0.03, 0.055, 0.08, 0.105, 0.13, 0.155, 0.18, 0.205])) or [1.0]
    total_steps = min(int(cfg.get('pyramid_add_steps', cfg.get('pyramid_steps', len(pyramid_weights)))), len(pyramid_weights))
    pyramid_step = int(state.get('pyramid_step', 0) or 0)
    current_units = state.get('current_units', 0.0)
    new_state = state.copy()

    if current_units >= limit_units - POSITION_EPSILON:
        return 0.0, new_state, ''

    anchor_price = state.get('pyramid_anchor_price') or state.get('last_add_price') or ma150 or current_price
    if ma150 is not None and ma150 > 0:
        anchor_price = state.get('pyramid_anchor_price') or ma150
    anchor_price = float(anchor_price) if anchor_price else float(current_price)

    start_units = state.get('pyramid_start_units')
    limit_anchor_units = state.get('pyramid_limit_units')
    init_qty = 0.0
    init_done = bool(state.get('target_reached_once', False))

    if start_units is None or float(start_units or 0.0) <= POSITION_EPSILON or limit_anchor_units is None or float(limit_anchor_units or 0.0) <= POSITION_EPSILON:
        if current_units < target_units - POSITION_EPSILON:
            init_qty = normalize_position_amount(target_units - current_units, mode, lot_size)
            start_units = target_units
            init_done = True
        else:
            start_units = current_units
            init_done = True
        limit_anchor_units = limit_units
        new_state['pyramid_start_units'] = start_units
        new_state['pyramid_limit_units'] = limit_anchor_units
        new_state['pyramid_anchor_price'] = anchor_price
        new_state['last_add_price'] = anchor_price
        new_state['target_reached_once'] = init_done
        new_state['pyramid_add_active'] = True

    start_units = float(start_units or 0.0)
    limit_anchor_units = float(limit_anchor_units or limit_units)
    pyramid_budget = max(limit_anchor_units - start_units, 0.0)

    ref_add_price = state.get('last_add_price') or anchor_price
    if pyramid_step > 0 and ref_add_price and float(ref_add_price) > 0:
        target_step = pyramid_step
        trigger_price = float(ref_add_price)
        for _step in range(pyramid_step + 1, total_steps + 1):
            trigger_price *= (1 - pyramid_add_step)
            if current_price <= trigger_price + POSITION_EPSILON:
                target_step = _step
            else:
                break
    else:
        target_step = _get_crossed_pyramid_step(anchor_price, current_price, pyramid_add_step, total_steps)
    target_step = min(max(target_step, pyramid_step), total_steps)

    step_qty = 0.0
    if init_done and pyramid_budget > POSITION_EPSILON and target_step > pyramid_step:
        step_weight = sum(float(w) for w in pyramid_weights[pyramid_step:target_step])
        step_qty = normalize_position_amount(pyramid_budget * step_weight, mode, lot_size)

    total_qty = normalize_position_amount(init_qty + step_qty, mode, lot_size)
    max_allowed = normalize_position_amount(limit_units - current_units, mode, lot_size)
    total_qty = normalize_position_amount(min(total_qty, max_allowed), mode, lot_size)

    if total_qty > POSITION_EPSILON:
        new_state['pyramid_step'] = target_step
        new_state['last_add_price'] = current_price
        new_state['pyramid_anchor_price'] = anchor_price
        new_state['pyramid_start_units'] = start_units
        new_state['pyramid_limit_units'] = limit_anchor_units
        new_state['target_reached_once'] = True
        new_state['pyramid_add_active'] = True
        if init_qty > POSITION_EPSILON and step_qty > POSITION_EPSILON:
            return total_qty, new_state, _format_step_reason('PYRAMID_INIT_STEP', pyramid_step + 1, target_step)
        if init_qty > POSITION_EPSILON:
            return total_qty, new_state, 'PYRAMID_INIT'
        return total_qty, new_state, _format_step_reason('PYRAMID_STEP', pyramid_step + 1, target_step)

    new_state['pyramid_anchor_price'] = anchor_price
    new_state['pyramid_start_units'] = start_units
    new_state['pyramid_limit_units'] = limit_anchor_units
    new_state['last_add_price'] = state.get('last_add_price') or anchor_price
    new_state['target_reached_once'] = init_done
    return 0.0, new_state, ''


def get_pyramid_sell_decision(state: Dict[str, Any], cfg: Dict[str, Any], current_price: float, mode: str, lot_size: int = 100) -> Tuple[float, Dict[str, Any], str]:
    """Clear 区倒金字塔卖出。
    
    规则：
    1. 首次进入 CLEAR_ZONE 时锁定锚点为当前价格
    2. 价格每上涨 step_pct，卖出对应权重的仓位
    3. 价格回跌到 BOX/CHANCE 时重置状态
    """
    clear_step_pct = float(cfg.get('clear_zone_step_percent', 0.04))
    if clear_step_pct <= 0:
        raise ValueError('clear_zone_step_percent must be > 0')
    
    sell_weights = cfg.get('pyramid_sell_weights', cfg.get('clear_pyramid_weights',
                       cfg.get('pyramid_add_weights', 
                       cfg.get('pyramid_weights', [0.03, 0.055, 0.08, 0.105, 0.13, 0.155, 0.18, 0.205])))) or [1.0]
    total_steps = min(int(cfg.get('pyramid_sell_steps', cfg.get('clear_pyramid_steps',
                       cfg.get('pyramid_add_steps',
                       cfg.get('pyramid_steps', len(sell_weights)))))), len(sell_weights))
    
    current_step = int(state.get('pyramid_sell_step', 0) or 0)
    current_units = state.get('current_units', 0.0)
    anchor_price = state.get('pyramid_sell_anchor_price', 0.0)
    new_state = state.copy()
    
    if anchor_price <= 0 or current_units <= POSITION_EPSILON:
        return 0.0, new_state, ''
    
    target_step = get_clear_pyramid_target_step(current_price, state, cfg, total_steps)
    target_step = min(max(target_step, current_step), total_steps)
    
    sell_qty = 0.0
    if target_step > current_step:
        weight_sum = sum(float(w) for w in sell_weights[current_step:target_step])
        sell_qty = normalize_position_amount(current_units * weight_sum, mode, lot_size)
    
    if sell_qty > POSITION_EPSILON:
        new_state['pyramid_sell_step'] = target_step
        new_state['pyramid_sell_last_price'] = current_price
        return sell_qty, new_state, _format_step_reason('PYRAMID_SELL_STEP', current_step + 1, target_step)
    
    return 0.0, new_state, ''


def get_box_fixed_add_decision(state: Dict[str, Any], cfg: Dict[str, Any], target_units: float, current_price: float, mode: str, lot_size: int = 100) -> Tuple[float, Dict[str, Any]]:
    """BOX 固定补仓；锚点改为 last_add_price 优先，避免历史高价导致连续触发。"""
    box_add_step = float(cfg.get('box_add_step', cfg.get('add_box_step', 0.04)))
    add_box_units_pct = float(cfg.get('add_box_units_percent', 0.1))
    if box_add_step <= 0:
        raise ValueError('box_add_step must be > 0')
    if add_box_units_pct <= 0:
        raise ValueError('add_box_units_percent must be > 0')

    last_add_price = state.get('last_add_price', 0.0) or 0.0
    last_trade_price = state.get('last_trade_price', 0.0) or 0.0
    initial_price = state.get('initial_entry_price', 0.0) or 0.0
    current_units = state.get('current_units', 0.0)
    anchor_price = last_add_price or last_trade_price or initial_price or current_price

    if current_price > anchor_price * (1 - box_add_step) + POSITION_EPSILON:
        return 0.0, state.copy()
    max_add = normalize_position_amount(target_units - current_units, mode, lot_size)
    if max_add <= POSITION_EPSILON:
        return 0.0, state.copy()
    add_qty = normalize_position_amount(target_units * add_box_units_pct, mode, lot_size)
    add_qty = normalize_position_amount(min(add_qty, max_add), mode, lot_size)
    new_state = state.copy()
    if add_qty > POSITION_EPSILON:
        new_state['last_add_price'] = current_price
    return add_qty, new_state


def get_add_trade_decision(state: Dict[str, Any], cfg: Dict[str, Any], target_units: float, limit_units: float, current_price: float, ma150: float, zone: str, mode: str, lot_size: int = 100) -> Tuple[float, str, Dict[str, Any], Dict[str, Any], List[str]]:
    """统一加仓入口；保留原逻辑：机会区启动后，BOX 区仍可延续倒金字塔加仓。"""
    new_state, cfg_updates, events, effective_mode = evaluate_pyramid_add_runtime(state, cfg, current_price, ma150, zone, target_units)
    if effective_mode == 'yes' and zone in {'CHANCE_ZONE', 'BOX_ZONE'}:
        add_qty, py_state, reason = get_pyramid_add_decision(new_state, cfg, target_units, limit_units, current_price, ma150, mode, lot_size)
        if add_qty > POSITION_EPSILON:
            merged = new_state.copy()
            merged.update(py_state)
            return add_qty, reason, merged, cfg_updates, events
        new_state = py_state
    return 0.0, '', new_state, cfg_updates, events


def get_sell_trade_decision(state: Dict[str, Any], cfg: Dict[str, Any], current_price: float, zone: str, mode: str, lot_size: int = 100) -> Tuple[float, str, Dict[str, Any], Dict[str, Any], List[str]]:
    """统一卖出入口。
    
    支持两种卖出模式：
    1. Clear 区倒金字塔卖出（pyramid_sell_flag 自动管理）
    2. 趋势区阶梯卖出（原有逻辑）
    """
    new_state, cfg_updates, events, effective_mode = evaluate_pyramid_sell_runtime(state, cfg, zone, current_price)
    
    if effective_mode == 'on' and zone == 'CLEAR_ZONE':
        sell_qty, py_state, reason = get_pyramid_sell_decision(new_state, cfg, current_price, mode, lot_size)
        if sell_qty > POSITION_EPSILON:
            merged = new_state.copy()
            merged.update(py_state)
            return sell_qty, reason, merged, cfg_updates, events
        new_state = py_state
    
    return 0.0, '', new_state, cfg_updates, events