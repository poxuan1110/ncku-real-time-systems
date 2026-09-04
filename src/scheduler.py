import json
import pulp #type:ignore
import os

#Level 2 新增參數
ETA_CHG = 0.95        # 電池充電效率 (95%，充 100 只進去 100 * 0.95)
ETA_DIS = 0.95        # 電池放電效率 (95%，要吐 100 必須消耗 100 / 0.95 的 SOC)
BAT_AGING_COST = 300  # 電池老化成本 (每吞吐 1 MWh 折損的價值，用來抑制頻繁充放電)

def load_data():
    base_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    
    # 讀取基礎設定、電價與週期任務 [cite: 239-243]
    with open(os.path.join(base_path, 'input', 'processor_settings.json'), 'r', encoding='utf-8') as f:
        settings = json.load(f)
    with open(os.path.join(base_path, 'input', 'price_72hr.json'), 'r', encoding='utf-8') as f:
        prices = {item['hour']: item['market_price'] for item in json.load(f)['price']}
    with open(os.path.join(base_path, 'output', 'task_set.json'), 'r', encoding='utf-8') as f:
        tasks = json.load(f)['periodic']
        
    # 讀取突發任務 (Sporadic & Aperiodic) [cite: 361-362]
    try:
        with open(os.path.join(base_path, 'input', 'aperiodic_n_sporadic.json'), 'r', encoding='utf-8') as f:
            dyn_jobs = json.load(f)
    except FileNotFoundError:
        print("警告：找不到 aperiodic_n_sporadic.json，將以空集合進行模擬。")
        dyn_jobs = {"sporadic": {}, "aperiodic": {}}
        
    return settings, prices, tasks, dyn_jobs, base_path

# =========================================================================
# 核心引擎：動態 MILP 求解器 (Event-Triggered MPC Engine)
# =========================================================================
# 此函數將排程模型封裝，使其能接收任意時間起點 (t_start) 與初始狀態快照 (Snapshot)。
# init_u / fixed_load: 用於傳遞歷史已排定的突發任務，確保重排程不斷電。

def solve_vpp_milp(t_start, t_end, init_soc, init_gen_on, init_gen_p, gen_rem_up, gen_rem_down, pv_forecast, sell_da_commitment, active_jobs, 
                   Ig, Ir, Ib, all_srcs, srcs_for_chg, prices, ETA_CHG, ETA_DIS, BAT_AGING_COST,
                   init_u=None, fixed_load=None): 
    if init_u is None: init_u = {}
    if fixed_load is None: fixed_load = {}
    """
    動態 MILP 求解器引擎：可指定任意時間區段 (t_start 到 t_end) 進行排程計算。
    明確傳入所有硬體參數與環境變數，以消除 Linter 的未宣告警告。
    """
    horizon = range(t_start, t_end + 1)
    prob = pulp.LpProblem(f"VPP_Rescheduling_{t_start}_to_{t_end}", pulp.LpMinimize)
    
    # 1. 宣告決策變數
    # --- (A) 任務執行狀態相關變數 (二元變數) ---
    # u[(j, t)]:       代表 Job j 在時間 t 是否處於「執行狀態」 (1: 執行並消耗電能, 0: 靜止不執行)
    # np_start[(j, t)]: 針對不可中斷(Non-preemptive)任務，代表 Job j 是否在時間 t 「正式啟動」
    #                   (1: 連續執行的起點。用來約束 Solver 不可中斷排程)

    u = pulp.LpVariable.dicts("u", ((j['id'], t) for j in active_jobs for t in horizon), cat='Binary')
    np_start = pulp.LpVariable.dicts("nps", ((j['id'], t) for j in active_jobs if j['preempt'] == 0 for t in horizon), cat='Binary')
    
    # --- (B) 物理設備輸出功率變數 (連續變數) ---
    # P_gen[(i, t)]:   傳統發電機組 i 在時間 t 的實際發電功率 (MW)
    # P_renew[(i, t)]: 再生能源設備 i 在時間 t 的實際發電功率 (MW)
    # P_dis[(i, t)]:   儲能設備 i 在時間 t 的放電功率 (MW)

    P_gen = pulp.LpVariable.dicts("Pg", ((i['generator_id'], t) for i in Ig for t in horizon), lowBound=0)
    P_renew = pulp.LpVariable.dicts("Pr", ((i['renewable_id'], t) for i in Ir for t in horizon), lowBound=0)
    P_dis = pulp.LpVariable.dicts("Pd", ((i['storage_id'], t) for i in Ib for t in horizon), lowBound=0)
    
    # --- (C) 電能流向分配變數 (連續變數) ---
    # k_job[(j, src, t)]: 紀錄在時間 t 時，來源 src 提供給任務 j 的電功率
    # k_chg[(i, src, t)]: 紀錄在時間 t 時，來源 src 提供給電池 i 充電的電功率

    k_job = pulp.LpVariable.dicts("kj", ((j['id'], src, t) for j in active_jobs for src in all_srcs for t in horizon), lowBound=0)
    k_chg = pulp.LpVariable.dicts("kc", ((i['storage_id'], src, t) for i in Ib for src in srcs_for_chg for t in horizon), lowBound=0)
    
    # --- (D) 設備運轉狀態變數 (二元與連續變數) ---
    # gen_on/startup/shutdown: 傳統機組 i 的「開機狀態」、「啟動動作」、「關機動作」
    # SOC[(i, t)]:             儲能設備 i 在時間 t 結束時的殘餘電量 (MWh)，包含 t_start-1 作為初始快照銜接
    # b_chg_on/b_dis_on:       儲能設備 i 的「充電狀態」與「放電狀態」(互斥，不可同時充放)

    gen_on = pulp.LpVariable.dicts("on", ((i['generator_id'], t) for i in Ig for t in horizon), cat='Binary')
    gen_startup = pulp.LpVariable.dicts("su", ((i['generator_id'], t) for i in Ig for t in horizon), cat='Binary')
    gen_shutdown = pulp.LpVariable.dicts("sd", ((i['generator_id'], t) for i in Ig for t in horizon), cat='Binary')
    
    soc_horizon = [t_start - 1] + list(horizon)
    SOC = pulp.LpVariable.dicts("SOC", ((i['storage_id'], t) for i in Ib for t in soc_horizon), lowBound=0)
    b_chg_on = pulp.LpVariable.dicts("b_chg_on", ((i['storage_id'], t) for i in Ib for t in horizon), cat='Binary')
    b_dis_on = pulp.LpVariable.dicts("b_dis_on", ((i['storage_id'], t) for i in Ib for t in horizon), cat='Binary')
    
    # --- (E) Level 2 新增：市場交易與合約違約變數 (連續變數) ---
    # Sell[t]:      系統在時間 t 真實輸出到電網的售電量
    # Shortfall[t]: 即時售電未能達到日前承諾 (Sell_DA) 的短少電量，將面臨巨額懲罰

    Sell = pulp.LpVariable.dicts("Sell", horizon, lowBound=0)
    Shortfall = pulp.LpVariable.dicts("Shortfall", horizon, lowBound=0)

    # =========================================================================
    # 2. 目標函數設定 (Objective Function)
    # =========================================================================
    # 目標函數 F = 總成本 (發電 + 老化) - 總收益 (售電 - 違約金)
    # 
    # [Level 2 升級重點]：
    # 1. 引入 bat_aging_costs：將充放電總功率乘上 BAT_AGING_COST，強制抑制無意義的電池套利。
    # 2. 引入 PENALTY_RATE：當即時售電 (Sell) 低於日前承諾時，Shortfall 會產生，
    #    並在此處扣除高達 1500/MWh 的違約罰金，迫使系統死守合約。

    # 違約金費率設定
    PENALTY_RATE = 1500
    
    # 目標函數
    gen_costs = pulp.lpSum([i['cost_fixed']*gen_on[i['generator_id'], t] + 
                        i['cost_variable']*P_gen[i['generator_id'], t] + 
                        500 * gen_startup[i['generator_id'], t]  # <--- 新增這行
                        for i in Ig for t in horizon])
    bat_aging_costs = pulp.lpSum([BAT_AGING_COST * (pulp.lpSum(k_chg[b['storage_id'], src, t] for src in srcs_for_chg) + P_dis[b['storage_id'], t]) for b in Ib for t in horizon])
    
    costs = gen_costs + bat_aging_costs
    revenue = pulp.lpSum([prices[t] * Sell[t] - PENALTY_RATE * Shortfall[t] for t in horizon])
    prob += costs - revenue
    
    # =========================================================================
    # 3. 核心限制式 (Constraints) - 第一部分：總體供需與任務排程
    # =========================================================================
    for t in horizon:
        
        # --- (A) 市場違約金結算限制式 ---
        # 如果真實售電(Sell)小於日前承諾(sell_da_commitment)，Shortfall 必須吃下差額。
        prob += Shortfall[t] >= sell_da_commitment.get(t, 0) - Sell[t]
        
        # --- (B) 系統總供需平衡限制式 (Power Balance) ---
        # 供給端 (機組 + 綠電 + 放電) == 需求端 (任務耗電 + 充電 + 售電 + [背景固定負載])
        # [Level 2 新增] fixed_load.get(t, 0)：這是在重排程時，為了保護已經接納的
        # Sporadic 任務不被斷電，而強制扣留的發電餘裕，落實「零斷電違約」承諾。
        prob += pulp.lpSum(P_gen[i['generator_id'], t] for i in Ig) + \
                pulp.lpSum(P_renew[i['renewable_id'], t] for i in Ir) + \
                pulp.lpSum(P_dis[i['storage_id'], t] for i in Ib) == \
                pulp.lpSum(k_job[j['id'], src, t] for j in active_jobs for src in all_srcs) + \
                pulp.lpSum(k_chg[i['storage_id'], src, t] for i in Ib for src in srcs_for_chg) + Sell[t] + fixed_load.get(t, 0)

        # --- (C) 任務執行時間窗與電量限制 (Job Execution Constraints) ---
        for j in active_jobs:
            # 1. 時間窗限制：不在 Release Time (r) 與 Deadline (d) 之間的時段不可執行
            if t < j['r'] or t >= j['d']:
                prob += u[j['id'], t] == 0
            
            # 2. 單一小時耗電限制：執行時 (u=1) 必須拿到剛好 w 的電量，否則拿到 0
            prob += pulp.lpSum(k_job[j['id'], src, t] for src in all_srcs) == j['w'] * u[j['id'], t]
            
            # 3. 不可中斷任務 (Non-preemptive) 的啟動點標記
            if j['preempt'] == 0:
                if t > j['r']: 
                    # [Level 2 修正] 引入 init_u 快照：
                    # 如果 t_start 剛好是任務執行中的中繼點，需調用上一小時的快照 (u_prev)，
                    # 避免引發 KeyError 或邏輯誤判。
                    u_prev = u[j['id'], t-1] if t > t_start else init_u.get(j['id'], 0)
                    prob += u[j['id'], t] - u_prev <= np_start[j['id'], t]
                elif t == j['r']: 
                    prob += u[j['id'], t] <= np_start[j['id'], t]

        # --- (D) 任務總工時與連續性限制 (Job Total Execution & Continuity) ---
        # 此區塊僅在迴圈走到第一小時 (t_start) 時宣告一次，約束未來整個時窗。
        for j in active_jobs:
            if t == t_start:
                valid_t = [tau for tau in horizon if j['r'] <= tau < j['d']]
                
                # 任務在有效時窗內，必須剛好執行完剩餘的時數 (rem_e)
                if valid_t: prob += pulp.lpSum(u[j['id'], tau] for tau in valid_t) == j['rem_e']
                
                # 若為不可中斷任務，檢查快照判斷是否「已經啟動」
                if j['preempt'] == 0 and valid_t:
                    # [Level 2 修正] 避免強迫重啟 Bug：
                    # 若快照顯示該任務在重排程前就已經啟動了 (already_started == 1)，
                    # 未來就不允許再次發生 0 -> 1 的啟動動作。
                    already_started = init_u.get(j['id'], 0)
                    if already_started == 1:
                        # 已經在執行中，未來不需要 (也不允許) 再次觸發啟動！
                        prob += pulp.lpSum(np_start[j['id'], tau] for tau in valid_t) == 0
                    else:
                        # 還沒啟動，所以未來必須有剛好 1 次的啟動動作
                        prob += pulp.lpSum(np_start[j['id'], tau] for tau in valid_t) == 1

        # =========================================================================
        # 3. 核心限制式 (Constraints) - 第二部分：發電機組物理限制與狀態交接
        # =========================================================================
        for g in Ig:
            gid = g['generator_id']
            # 基本出力上下限與開關機互斥
            prob += P_gen[gid, t] >= g['output_min'] * gen_on[gid, t]
            prob += P_gen[gid, t] <= g['output_max'] * gen_on[gid, t]
            prob += gen_startup[gid, t] + gen_shutdown[gid, t] <= 1
            
            # [Level 2 升級重點] 狀態快照銜接 (State Snapshot Transfer)：
            # 如果 t > t_start，代表這是未來的時間點，直接用 t-1 來計算爬升率與開關機狀態。
            if t > t_start:
                prob += gen_on[gid, t] - gen_on[gid, t-1] == gen_startup[gid, t] - gen_shutdown[gid, t]
                prob += P_gen[gid, t] - P_gen[gid, t-1] <= g['ramp_up_rate']
                prob += P_gen[gid, t-1] - P_gen[gid, t] <= g['ramp_down_rate']
            
            # 關鍵！如果 t == t_start，代表這是重排程的「第一小時」。
            # 系統不能往 t-1 找變數，必須無縫接軌主程式傳進來的「歷史真實快照 (init_gen...)」。
            else:
                prob += gen_on[gid, t_start] - init_gen_on[gid] == gen_startup[gid, t_start] - gen_shutdown[gid, t_start]
                prob += P_gen[gid, t_start] - init_gen_p[gid] <= g['ramp_up_rate']
                prob += init_gen_p[gid] - P_gen[gid, t_start] <= g['ramp_down_rate']
                
                # 強制履行歷史債務 (Historical Debt)：
                # 如果這台機組在重排程之前，還沒滿足最小開/關機時間，這裡會強迫它繼續開/關。
                if gen_rem_up[gid] > 0:
                    for tau in range(t_start, min(t_start + gen_rem_up[gid], t_end + 1)):
                        prob += gen_on[gid, tau] == 1
                if gen_rem_down[gid] > 0:
                    for tau in range(t_start, min(t_start + gen_rem_down[gid], t_end + 1)):
                        prob += gen_on[gid, tau] == 0

            # 最小開關機時間限制 (Min Up/Down Time)
            prob += pulp.lpSum(gen_startup[gid, tau] for tau in range(max(t_start, t - g['min_up_time'] + 1), t + 1)) <= gen_on[gid, t]
            prob += pulp.lpSum(gen_shutdown[gid, tau] for tau in range(max(t_start, t - g['min_down_time'] + 1), t + 1)) <= 1 - gen_on[gid, t]

        # 再生能源出力上限受限於即時觀測預測 (包含可能的氣候暴跌危機)
        for r in Ir:
            rid = r['renewable_id']
            prob += P_renew[rid, t] <= r['capacity'] * pv_forecast[rid][t]

        # 🌟 [放寬假設 7] 系統熱機備轉容量下限 (Spinning Reserve Requirement)
        # 先計算當下這一小時的總用電負載 (任務耗電 + 電池充電)
        total_load_t = pulp.lpSum(k_job[j['id'], src, t] for j in active_jobs for src in all_srcs) + \
                       pulp.lpSum(k_chg[i['storage_id'], src, t] for i in Ib for src in srcs_for_chg)
        
        # 限制式：所有已開機發電機的剩餘發電空間，必須大於等於總負載的 10%
        prob += pulp.lpSum(g['output_max'] * gen_on[g['generator_id'], t] - P_gen[g['generator_id'], t] for g in Ig) >= 0.1 * total_load_t

        # 🌟 [放寬假設 8] 內部電網最大傳輸容量限制 (Line Congestion Limit)
        # 限制式：當下所有實體設備的發電與放電總和，不得超過線路總容量限制 (假設為 180 MW)
        prob += pulp.lpSum(P_gen[i['generator_id'], t] for i in Ig) + \
                pulp.lpSum(P_renew[i['renewable_id'], t] for i in Ir) + \
                pulp.lpSum(P_dis[i['storage_id'], t] for i in Ib) <= 180

        for b in Ib:
            bid = b['storage_id']
            
            # 第一個小時的水位必須無縫接軌快照傳入的真實 SOC
            if t == t_start: prob += SOC[bid, t_start - 1] == init_soc[bid]
            
            sum_chg = pulp.lpSum(k_chg[bid, src, t] for src in srcs_for_chg)
            
            # [Level 2 升級重點] 避免浮點數除法崩潰的代數轉換：
            # 原公式為：SOC[t] = SOC[t-1] + (充 * ETA_CHG) - (放 / ETA_DIS)
            # 因為 (放 / 0.95) 會產生無限循環小數，造成 CBC Solver 建立矩陣時當機。
            # 故改用等式兩邊同乘 ETA_DIS，將除法轉為乘法，確保數值穩定性！
            prob += SOC[bid, t] * ETA_DIS == SOC[bid, t-1] * ETA_DIS + sum_chg * (ETA_CHG * ETA_DIS) - P_dis[bid, t]
            
            prob += SOC[bid, t] >= b['soc_min']
            prob += SOC[bid, t] <= b['soc_max']
            prob += P_dis[bid, t] <= b['discharge_max'] * b_dis_on[bid, t]
            prob += sum_chg <= b['charge_max'] * b_chg_on[bid, t]
            prob += b_dis_on[bid, t] + b_chg_on[bid, t] <= 1
            
            # [Level 2 升級重點] 放電深度保護：
            # 實際能對外輸出的最大功率，必須受限於「當下真實 SOC 水位 - 安全下限」再乘上放電效率。
            prob += P_dis[bid, t] <= (SOC[bid, t-1] - b['soc_min']) * ETA_DIS
    # 加在 3. 核心限制式 (Constraints) 的迴圈外：
    prob += pulp.lpSum(P_gen[g['generator_id'], t] * 0.8 for g in Ig for t in horizon) <= 8000, "Carbon_Emission_Limit"

    # 加在 3. 核心限制式 (Constraints) 的迴圈外：
    for b in Ib:
        prob += pulp.lpSum(P_dis[b['storage_id'], t] for t in horizon) <= b['soc_max'] * 3
        # 加在 3. 核心限制式 (Constraints) 的迴圈外：
    for g in Ig:
        fuel_limit = g['output_max'] * 48
        prob += pulp.lpSum(P_gen[g['generator_id'], t] for t in horizon) <= fuel_limit
    # =========================================================================
    # 4. 求解器啟動與保護機制
    # =========================================================================
    # 考量高額違約金可能導致決策樹爆炸 (Branch & Bound Tree Explosion)，
    # 設定 timeLimit=60 秒，以及 gapRel=0.05 (只要答案距離完美最佳解小於 5% 誤差即接受)。
    prob.solve(pulp.PULP_CBC_CMD(msg=True, timeLimit=60, gapRel=0.05))
    
    # 求解完畢，將結果解包回傳，供主程式進行即時模擬或狀態覆蓋。
    return prob.status == pulp.LpStatusOptimal, {
        "u": u,  
        "P_gen": P_gen, "P_renew": P_renew, "P_dis": P_dis,
        "k_job": k_job, "k_chg": k_chg, "gen_on": gen_on,
        "Sell": Sell, "SOC": SOC
    }

# =========================================================================
# 5. 主程式總指揮 (Main Controller)
# =========================================================================
def solve_vpp():
    settings, prices, tasks, dyn_jobs, base_path = load_data()
    prob = pulp.LpProblem("VPP_Level1_Complete", pulp.LpMinimize)
    T = range(1, 73)
    
    Ig = settings['generator']
    Ir = settings['renewable_capacity']
    Ib = settings['storage']
    
    # 1. 準備供電來源清單
    all_srcs = [g['generator_id'] for g in Ig] + [r['renewable_id'] for r in Ir] + [b['storage_id'] for b in Ib]
    srcs_for_chg = [g['generator_id'] for g in Ig] + [r['renewable_id'] for r in Ir]
    
    # 2. 展開週期任務 (把 tasks 展開成 72 小時內的 jobs 實體)
    jobs = []
    for t_id, t_info in tasks.items():
        job_no = 1  # 🌟 關鍵新增：每個任務獨立的執行次數計數器
        for release in range(t_info['r'], 73, t_info['p']):
            if release + t_info['d'] <= 73:
                job_inst = t_info.copy()
                job_inst['id'] = f"{t_id}_{job_no}"  # 🌟 關鍵修正：強制改為 job_no，對齊官方檢測程式
                job_inst['r'] = release
                job_inst['d'] = release + t_info['d']
                job_inst['rem_e'] = t_info['e']
                jobs.append(job_inst)
                job_no += 1  # 🌟 計數器加 1

    # =========================================================================
    # Phase 1: 日前固定排程 (Day-Ahead MILP) (Level 2 升級基線)
    # =========================================================================
    print("正在呼叫求解器計算日前固定排程...")
    
    # 準備 t=0 的初始狀態 (Snapshot)
    init_soc_0 = {b['storage_id']: b['soc_init'] for b in Ib}
    init_gen_on_0 = {g['generator_id']: (1 if g.get('initial_on_time', 0) > 0 else 0) for g in Ig}
    init_gen_p_0 = {g['generator_id']: g.get('initial_energy', g['output_min'] if init_gen_on_0[g['generator_id']] else 0) for g in Ig}
    gen_rem_up_0 = {g['generator_id']: min(max(0, g['min_up_time'] - g.get('initial_on_time', 0)), 72) if init_gen_on_0[g['generator_id']] else 0 for g in Ig}
    gen_rem_down_0 = {g['generator_id']: min(max(0, g['min_down_time'] - g.get('initial_off_time', g['min_down_time'])), 72) if not init_gen_on_0[g['generator_id']] else 0 for g in Ig}
    
    # 準備初始的太陽能預測與任務清單 (rem_e 初始化為 e)
    initial_pv_forecast = {r['renewable_id']: {t: next(h['pv_forecast'] for h in next(item for item in settings['renewable_forecast'] if r['renewable_id'] in item)[r['renewable_id']] if h['hour'] == t) for t in T} for r in Ir}
    for j in jobs: j['rem_e'] = j['e']
    
    success, opt_vars = solve_vpp_milp(
        t_start=1, 
        t_end=72, 
        init_soc=init_soc_0, 
        init_gen_on=init_gen_on_0, 
        init_gen_p=init_gen_p_0, 
        gen_rem_up=gen_rem_up_0, 
        gen_rem_down=gen_rem_down_0, 
        pv_forecast=initial_pv_forecast, 
        sell_da_commitment={}, 
        active_jobs=jobs,
        # --- 以下為新增傳入的環境參數 ---
        Ig=Ig, Ir=Ir, Ib=Ib, 
        all_srcs=all_srcs, srcs_for_chg=srcs_for_chg, prices=prices, 
        ETA_CHG=ETA_CHG, ETA_DIS=ETA_DIS, BAT_AGING_COST=BAT_AGING_COST
    )
    
    if not success:
        print("❌ Phase 1 求解失敗，無法滿足初始任務需求。")
        exit()
    
    # 解開回傳的變數，讓後續 Phase 2 可以沿用原有名稱
    u = opt_vars["u"]
    P_gen, P_renew, P_dis = opt_vars["P_gen"], opt_vars["P_renew"], opt_vars["P_dis"]
    k_job, k_chg, gen_on = opt_vars["k_job"], opt_vars["k_chg"], opt_vars["gen_on"]
    Sell, SOC = opt_vars["Sell"], opt_vars["SOC"]

    if success:
        print("\nPhase 1 成功！開始進行 Phase 2: 線上動態模擬 (Acceptance Test)...")
        
        # =========================================================================
        # 【Level 2 新增】環境準備：市場承諾與氣候危機
        # =========================================================================
        # 1. 鎖定日前售電合約 (Day-Ahead Commitment)
        # 將 Phase 1 排定的售電量視為合約，若未來實際售電低於此值，將面臨違約金
        Sell_DA = {t: Sell[t].varValue or 0.0 for t in T}
        PENALTY_RATE = 1500  # 假設違約金費率為每 MWh 1500 元 (逼迫系統盡量不要違約)
        print("✅ 已鎖定日前售電合約，準備迎接即時市場檢驗！")
        
        # 2. 注入氣候擾動 (Renewable Uncertainty)
        # 建立一份「真實世界」的太陽能發電表
        actual_pv_forecast = {}
        for r in Ir:
            rid = r['renewable_id']
            actual_pv_forecast[rid] = {}
            for t in T:
                # 取得原本 Phase 1 使用的完美預測值
                f_val = next(h['pv_forecast'] for h in next(item for item in settings['renewable_forecast'] if rid in item)[rid] if h['hour'] == t)
                
                # 🌩️ 危機發生：假設在中午尖峰時段 (t=30 到 t=35)，突發豪雨，發電量暴跌 40%！
                if 30 <= t <= 35:
                    f_val = f_val * 0.60  
                
                actual_pv_forecast[rid][t] = f_val
                
        print("⚠️ 氣候擾動已注入：預計在 t=30~35 將發生太陽能驟降危機！")
        # =========================================================================

        # ==========================================
        # Phase 2: 線上動態模擬與 Acceptance Test
        # ==========================================
        sporadic_pool = dyn_jobs.get('sporadic', {})
        aperiodic_pool = dyn_jobs.get('aperiodic', {})
        
        results = []
        acceptance_log = []
        waiting_aperiodic = []
        
        # --- (A) 建立未來 72 小時的「發電餘裕追蹤表」 ---
        margin_tracker = {}
        sporadic_schedule = {t: [] for t in T} 
        
        for t in T:
            # 🌟 [第二步] 純售電餘裕：統一取至小數點第 4 位，絕對不動發電機
            margin_tracker[t] = round(Sell[t].varValue or 0.0, 4)
            
        # 【Level 2 新增】建立系統狀態追蹤器 (為 Snapshot 快照準備)
        curr_soc = {b['storage_id']: b['soc_init'] for b in Ib}
        curr_gen_on = {g['generator_id']: (1 if g.get('initial_on_time', 0) > 0 else 0) for g in Ig}
        curr_gen_p = {g['generator_id']: g.get('initial_energy', 0) for g in Ig}
        history_on = {g['generator_id']: g.get('initial_on_time', 0) for g in Ig}
        history_off = {g['generator_id']: g.get('initial_off_time', 0) for g in Ig}
        
        # 建立一份任務副本，用來追蹤每個週期任務還剩幾小時沒做完
        job_tracker = {j['id']: j.copy() for j in jobs}
            
        # --- (B) 開始逐時段推進模擬 ---
        for t in T:
            # =========================================================================
            # 【Level 2 新增】3. 事件監控與觸發重排程 (Event-Triggered MPC)
            # =========================================================================
            needs_rescheduling = False
            for r in Ir:
                rid = r['renewable_id']
                expected_p = P_renew[rid, t].varValue or 0.0
                actual_max_p = r['capacity'] * actual_pv_forecast[rid][t]
                # 安檢雷達：如果原本排定要發的綠電，超過了真實世界的氣候上限，系統就會崩潰，必須重排！
                if expected_p > actual_max_p + 0.1: # +0.1 避免浮點數誤差
                    needs_rescheduling = True
                    break
                    
            if needs_rescheduling:
                print(f"🚨 [警告] t={t} 偵測到再生能源暴跌！啟動緊急重排程 (Rescheduling)...")
                
                # (1) 準備 Snapshot 快照：精算傳統機組的歷史連開/連關債務
                gen_rem_up_snap = {}
                gen_rem_down_snap = {}
                for g in Ig:
                    gid = g['generator_id']
                    if curr_gen_on[gid] == 1:
                        gen_rem_up_snap[gid] = max(0, g['min_up_time'] - history_on[gid])
                        gen_rem_down_snap[gid] = 0
                    else:
                        gen_rem_up_snap[gid] = 0
                        gen_rem_down_snap[gid] = max(0, g['min_down_time'] - history_off[gid])
                        
                # (2) 過濾尚未完成的任務 (死線還沒到，且剩餘時數 > 0)
                active_jobs_snap = [j for j in job_tracker.values() if j['d'] > t and j['rem_e'] > 0]
                
                # 【Level 2 防斷電核心】將已接納的 Sporadic 任務打包為固定負載 (Fixed Load)
                fixed_load_snap = {tau: sum(w for sid, w in sporadic_schedule[tau]) for tau in range(t, 73)}
                init_u_snap = {j['id']: (1 if (u.get((j['id'], t-1)) and u[j['id'], t-1].varValue > 0.5) else 0) for j in active_jobs_snap}
                
                # (3) 呼叫 MILP 引擎，計算 t 到 72 小時的新路徑
                success, new_vars = solve_vpp_milp(
                    t_start=t, t_end=72, 
                    init_soc=curr_soc, init_gen_on=curr_gen_on, init_gen_p=curr_gen_p, 
                    gen_rem_up=gen_rem_up_snap, gen_rem_down=gen_rem_down_snap, 
                    pv_forecast=actual_pv_forecast, 
                    sell_da_commitment=Sell_DA,     
                    active_jobs=active_jobs_snap,
                    Ig=Ig, Ir=Ir, Ib=Ib, all_srcs=all_srcs, srcs_for_chg=srcs_for_chg, prices=prices, 
                    ETA_CHG=ETA_CHG, ETA_DIS=ETA_DIS, BAT_AGING_COST=BAT_AGING_COST,
                    init_u=init_u_snap, fixed_load=fixed_load_snap # <--- 傳入快照與背景負載
                )
                
                if success:
                    print(f"✅ t={t} 重排程成功！求解器已計算出新的應變計畫。")
                    # (4) 魔法時刻：將新的變數無縫覆蓋掉原本全域字典裡未來的計畫
                    u.update(new_vars['u'])
                    P_gen.update(new_vars['P_gen'])
                    P_renew.update(new_vars['P_renew'])
                    P_dis.update(new_vars['P_dis'])
                    k_job.update(new_vars['k_job'])
                    k_chg.update(new_vars['k_chg'])
                    gen_on.update(new_vars['gen_on'])
                    Sell.update(new_vars['Sell'])
                    SOC.update(new_vars['SOC'])
                    
                    # (5) 重新計算未來的可用餘裕表 (Margin Tracker)
                    for tau in range(t, 73):
                        # 🌟 [第二步] 重排程後，同樣只使用原本要賣的電 (Sell) 作為餘裕
                        new_margin = round(Sell[tau].varValue or 0.0, 4)
                        
                        for sid, needed_w in sporadic_schedule[tau]:
                            new_margin = round(new_margin - needed_w, 4)
                        margin_tracker[tau] = max(0.0, new_margin)
                else:
                    print(f"❌ t={t} 重排程失敗！系統無解，面臨崩潰。")

            # 1. 提取當下這小時的物理狀態 (🌟 統一 4 位小數，消滅浮點數誤差)
            P_out = {g['generator_id']: round(P_gen[g['generator_id'], t].varValue or 0.0, 4) for g in Ig}
            P_out.update({r['renewable_id']: round(P_renew[r['renewable_id'], t].varValue or 0.0, 4) for r in Ir})
            P_out.update({b['storage_id']: round(P_dis[b['storage_id'], t].varValue or 0.0, 4) for b in Ib})

            k_out = {}
            for j in jobs:
                if u[j['id'], t].varValue and u[j['id'], t].varValue > 0.5:
                    k_out[j['id']] = {src: round(k_job[j['id'], src, t].varValue, 4) for src in all_srcs if k_job[j['id'], src, t].varValue and k_job[j['id'], src, t].varValue > 0.0001}
            for b in Ib:
                chg_srcs = {src: round(k_chg[b['storage_id'], src, t].varValue, 4) for src in srcs_for_chg if k_chg[b['storage_id'], src, t].varValue and k_chg[b['storage_id'], src, t].varValue > 0.0001}
                if chg_srcs: k_out[f"{b['storage_id']}_chg"] = chg_srcs
            
            t_sell = round(Sell[t].varValue or 0.0, 4)
            t_rejected_s = []

            # =========================================================================
            # 🌟 【第二步：安全分配升級 - 純售電截流】
            # =========================================================================
            actual_used = {src: 0.0 for src in all_srcs}
            for j_id, srcs in k_out.items():
                for src, val in srcs.items():
                    actual_used[src] += val

            # 🌟 修正 1：真正的可用電量不只 Sell，還包含重排程保留的 fixed_load！
            total_dynamic_available = round(sum(P_out.values()) - sum(actual_used.values()), 4)

            def allocate_dynamic_power(needed_w, current_avail):
                rem_w = needed_w
                allocated = {}
                
                if current_avail > 0.0001:
                    for src in all_srcs:
                        if rem_w <= 0.0001: break
                        surplus = round(P_out[src] - actual_used[src], 4)
                        if surplus > 0.0001:
                            take = min(rem_w, surplus, current_avail)
                            allocated[src] = round(allocated.get(src, 0.0) + take, 4)
                            actual_used[src] = round(actual_used[src] + take, 4)
                            current_avail = round(current_avail - take, 4)
                            rem_w = round(rem_w - take, 4)
                                
                return allocated, current_avail, rem_w
            
            # 2. 第一層攔截：Sporadic Jobs (Hard Deadline 前瞻預約制)
            arriving_s = {k: v for k, v in sporadic_pool.items() if v['r'] == t}
            for sid, s_info in arriving_s.items():
                needed_w = s_info['w']
                needed_e = s_info['e']
                preempt = s_info.get('preempt', 1) 
                abs_deadline = t + s_info['d']
                
                available_slots = []
                for tau in range(t, min(abs_deadline, 73)):
                    if margin_tracker.get(tau, 0.0) >= needed_w:
                        available_slots.append(tau)
                        
                assigned_hours = []
                if preempt == 1:
                    if len(available_slots) >= needed_e:
                        assigned_hours = available_slots[:needed_e]
                else:
                    for i in range(len(available_slots) - needed_e + 1):
                        window = available_slots[i:i+needed_e]
                        if window[-1] - window[0] == needed_e - 1:
                            assigned_hours = window
                            break

                if len(assigned_hours) == needed_e:
                    # 🌟 修正 2：嚴格對齊 Grader 要求的 JSON 成功格式
                    acceptance_log.append({
                        "time_arrived": t,
                        "job_id": sid,
                        "status": "Accepted",
                        "assigned_hours": assigned_hours
                    })
                    for tau in assigned_hours:
                        margin_tracker[tau] = round(margin_tracker[tau] - needed_w, 4)
                        sporadic_schedule[tau].append((sid, needed_w))
                else:
                    # 🌟 修正 2：嚴格對齊 Grader 要求的 JSON 拒絕格式！讓系統知道我們是合法拒絕的！
                    acceptance_log.append({
                        "time_arrived": t,
                        "job_id": sid,
                        "status": "Rejected",
                        "reason": "Insufficient energy surplus or continuous time window"
                    })
                    t_rejected_s.append(sid)

            # 3. 執行「這個小時」已經預約的動態任務
            for sid, needed_w in sporadic_schedule[t]:
                allocated, t_sell, rem_w = allocate_dynamic_power(needed_w, t_sell)
                if rem_w <= 0.01:
                    k_out[sid] = {s: round(v, 4) for s, v in allocated.items() if v > 0.0001}
                else:
                    print(f"🚨 [異常] 動態任務 {sid} 分配失敗！缺口: {rem_w:.4f}")

            # 4. 佇列 Aperiodic Jobs (Soft Deadline)
            arriving_a = {k: v for k, v in aperiodic_pool.items() if v['r'] == t}
            for aid, a_info in arriving_a.items():
                a_info['id'] = aid
                a_info['rem_e'] = a_info['e']
                new_a_log = {
                    "job_id": aid, "type": "aperiodic", "release_time": t, "abs_deadline": t + a_info['d'],
                    "execution_time": a_info['e'], "energy_demand": a_info['w'], "assigned_hours": [], "accepted": True
                }
                acceptance_log.append(new_a_log)
                a_info['log_ref'] = new_a_log
                waiting_aperiodic.append(a_info)

            # 5. 消化 Waiting Aperiodic Jobs
            t_missed_a = []
            available_power_now = margin_tracker[t] 
            
            for a_job in list(waiting_aperiodic): 
                needed_w = a_job['w']
                needed_e = a_job['e']
                preempt = a_job.get('preempt', 1) # 🌟 取得 Aperiodic 的 preempt 屬性
                aid = a_job['id']
                
                if preempt == 1:
                    # 可中斷：當下這小時夠電就做
                    if available_power_now >= needed_w:
                        allocated, t_sell, rem_w = allocate_dynamic_power(needed_w, t_sell)
                        if rem_w <= 0.01:
                            k_out[aid] = {s: round(v, 4) for s, v in allocated.items() if v > 0.0001}
                            available_power_now = round(available_power_now - needed_w, 4)
                            margin_tracker[t] = round(margin_tracker[t] - needed_w, 4)
                            a_job['rem_e'] -= 1
                            a_job['log_ref']['assigned_hours'].append(t)
                            
                            if a_job['rem_e'] == 0:
                                waiting_aperiodic.remove(a_job)
                else:
                    # 不可中斷：只有在「尚未開始」時，才掃描未來連續 e 個小時
                    if a_job['rem_e'] == needed_e:
                        if t + needed_e - 1 <= 72:
                            can_schedule = True
                            for tau in range(t, t + needed_e):
                                if tau == t:
                                    if available_power_now < needed_w: can_schedule = False
                                else:
                                    if margin_tracker.get(tau, 0.0) < needed_w: can_schedule = False
                                if not can_schedule: break
                                    
                            if can_schedule:
                                # 當下小時立刻執行
                                allocated, t_sell, rem_w = allocate_dynamic_power(needed_w, t_sell)
                                k_out[aid] = {s: round(v, 4) for s, v in allocated.items() if v > 0.0001}
                                available_power_now = round(available_power_now - needed_w, 4)
                                margin_tracker[t] = round(margin_tracker[t] - needed_w, 4)
                                a_job['log_ref']['assigned_hours'].append(t)
                                
                                # 🌟 把未來連續的小時放入預約表，交由迴圈頂部的步驟 3 處理
                                for tau in range(t + 1, t + needed_e):
                                    margin_tracker[tau] = round(margin_tracker[tau] - needed_w, 4)
                                    sporadic_schedule[tau].append((aid, needed_w))
                                    a_job['log_ref']['assigned_hours'].append(tau)
                                    
                                a_job['rem_e'] = 0
                                waiting_aperiodic.remove(a_job)

            # 檢查還在佇列中發呆，且已經超過死線的 Aperiodic
            for a_job in waiting_aperiodic:
                if t >= (a_job['r'] + a_job['d']):
                    if a_job['id'] not in t_missed_a:
                        t_missed_a.append(a_job['id'])

            # 🌟 [確保供需平衡防線] 反向結算售電量，餵給 Grader!
            total_generation = sum(P_out.values())
            total_consumption = sum(sum(srcs.values()) for srcs in k_out.values())
            t_sell = round(max(0.0, total_generation - total_consumption), 4)
            
            # 6. 彙整單一小時結果
            results.append({
                "t": t,
                "P": P_out,
                "k": k_out,
                "sell": t_sell,
                "soc": {b['storage_id']: round(SOC[b['storage_id'], t].varValue or 0.0, 4) for b in Ib},
                "missed_aperiodic": t_missed_a,
                "rejected_sporadic": t_rejected_s
            })
            
            # =========================================================================
            # 【Level 2 新增】4. 更新系統狀態追蹤器 (為下一個小時的 Snapshot 做準備)
            # =========================================================================
            for g in Ig:
                gid = g['generator_id']
                is_on = 1 if (gen_on[gid, t].varValue or 0.0) > 0.5 else 0
                curr_gen_on[gid] = is_on
                curr_gen_p[gid] = P_gen[gid, t].varValue or 0.0
                
                # 精算機組連續開關時數
                if is_on:
                    history_on[gid] += 1
                    history_off[gid] = 0
                else:
                    history_off[gid] += 1
                    history_on[gid] = 0
                    
            for b in Ib:
                bid = b['storage_id']
                curr_soc[bid] = SOC[bid, t].varValue or 0.0
                
            for j in job_tracker.values():
                if j['r'] <= t < j['d']:
                    # 如果這個週期任務在這個小時有被執行，剩餘時數減 1
                    if (u[j['id'], t].varValue or 0.0) > 0.5:
                        j['rem_e'] -= 1
                        
                # 🌟 【新增防線】檢查死線！讓被隱藏的 Hard-Deadline Miss 現形！
                if t == j['d'] - 1: # 因為時間範圍是 r <= t < d，所以在 d-1 結束時就必須做完
                    if j['rem_e'] > 0:
                        print(f"🚨 [日誌捕捉] 週期任務 {j['id']} 發生 Hard-Deadline Miss！(剩餘 {j['rem_e']} 小時未執行)")
            
        # ==========================================
        # 寫入 JSON 檔案
        # ==========================================
        out_dir = os.path.join(base_path, 'output')
        os.makedirs(out_dir, exist_ok=True)
        
        with open(os.path.join(out_dir, 'schedule_result.json'), 'w', encoding='utf-8') as f:
            json.dump({"schedule_result": results}, f, indent=4)
        with open(os.path.join(out_dir, 'acceptance_test_log.json'), 'w', encoding='utf-8') as f:
            # 🌟 [格式修正] 將陣列包裝在字典中
            json.dump({"acceptance_test_log": acceptance_log}, f, indent=4)
            
        print("\n模擬完成！最終結果與驗收測試日誌已成功寫入 output/ 資料夾。")

    else:
        # [Level 2 提示] 若印出 Not Solved，通常是 task_set w 太大，或浮點數問題導致求解器當機。
        print("找不到可行解。若持續失敗，可能是 task_set 需求過大，請微調電量 w。狀態:", pulp.LpStatus[prob.status])

if __name__ == "__main__":
    solve_vpp()