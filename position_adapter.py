def fixed_fractional(equity,risk_pct,entry,stop,side,contract_value):
    return {"dollar_risk":round(equity*risk_pct/100,2)}
