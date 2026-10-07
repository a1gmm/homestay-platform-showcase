"""Read-only accounting distinctions; never classify or confirm a transaction."""
import re


def business_explanation(text):
    if re.search(r"确认执行|改成|改为|记为|归类为|补齐|补录|立即执行", text):
        return None
    if not re.search(r"一回事|等同|区别|差别|一样|当成|等于|区分", text):
        return None
    if not re.search(r"吗|是否|是不是|有什么|什么区别|怎么|如何|[?？]", text):
        return None
    if re.search(r"携程|抖音|平台|OTA", text, re.I) and re.search(r"银行|到账|现金", text):
        return dict(
            message="不能直接等同。平台结算金额说明平台按账单结算了多少；银行到账说明账户实际收到了多少、何时收到。需要用结算批次、净额和银行流水逐笔核对，跨月到账、退款及手续费也要分别确认。缺少银行流水时，只能列为待核实到账，不能视为已收款或把未知现金写成零。",
            details=[dict(label="经营月份", value="按订单履约和已核实的业务归属统计；银行到账日期不自动改变经营月份。"),
                     dict(label="避免重复计算", value="平台月汇总、每日到账和订单明细可能描述同一批业务，不能再与银行入账重复计为多份收入。")],
        )
    if re.search(r"保洁|打扫", text) and re.search(r"阿姨|供应商|实际付|实付", text) and "业主" in text:
        return dict(
            message="不是一回事。向业主收取的保洁费，是按订单和结算规则计入业主账单的费用；实际付给保洁人员或供应商的钱，是公司的实际支出，需要核对供应商账单及付款凭证。两者金额和结算时间可能不同，不能用业主收费或打扫次数直接代替实际成本，也不能重复计入同一笔支出。",
            details=[dict(label="先分别核对", value="一边核对每次打扫与业主收费记录，另一边核对供应商应付、实际付款及业务月份，再检查对应关系。"),
                     dict(label="付款与承担方", value="公司先付款不代表费用最终由公司承担；业主承担也不代表业主已经付款。")],
        )
    return None
