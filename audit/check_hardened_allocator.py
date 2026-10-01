import math, logging
from datetime import datetime, timedelta, timezone
import numpy as np
from scipy.stats import nbinom
from prop_alpha.modeling.engine import *
from prop_alpha.scanner.market_scanner import *
from hardened_allocator import *
logging.disable(logging.CRITICAL)
NOW = datetime(2026,10,1,12,tzinfo=timezone.utc)
st = LineupExpectation(1.0,0.0)
def Q(fid,pid,line,o,u,ms=1e9,book="A",at=NOW):
    return LiveOddsQuote(fid,pid,"tackles",line,o,u,ms,book,at,VoidRule.VOID_IF_NO_APPEARANCE)
post = GammaPosterior(29.2, 11.944)
fps = {(9,i,"tackles"): FactorProjection(9,i,"tackles",post,6.0,st) for i in range(1,5)}

# 1. quadrature marginal == engine moment-matched NB to ~1e-3 and == MC
sp = fps[(9,1,"tackles")].side_probabilities(1.5,"over")
eng = PropProjection(9,1,"tackles",*QuantEngine(game_state_k=6.0).predictive_gamma(post,1.0),st).side_probabilities(1.5,"over")
rng=np.random.default_rng(1); n=2_000_000
L=rng.gamma(29.2,1/11.944,n); G=rng.gamma(6,1/6,n); mc=np.mean(rng.poisson(L*G*85/90)>1.5)
print(f"P(over1.5) quadrature={sp.p_win:.5f} MC={mc:.5f} moment-matched={eng.p_win:.5f}")
assert abs(sp.p_win-mc)<1.5e-3

# 2. cross-player covariance vs MC (shared G)
price=1.714
cands=[]
for i in range(1,5):
    c,_ = None,None
m = HardenedAssetManager(1e6, model_risk_loss_floor=0.0)
rep=HardenedReport()
cs=[m._candidates(fps[(9,i,"tackles")],[Q(9,i,1.5,price,None)],rep,(9,i,"tackles")) for i in range(1,5)]
mu,cov = fixture_moments([fps[(9,i,"tackles")] for i in range(1,5)], cs)
G=rng.gamma(6,1/6,n); R=[]
for i in range(4):
    x=rng.poisson(rng.gamma(29.2,1/11.944,n)*G*85/90); R.append(np.where(x>1.5,price-1,-1.0))
R=np.array(R); mc_cov=np.cov(R)
print("corr model:", np.round(cov[0,1]/cov[0,0],4), " MC:", np.round(mc_cov[0,1]/mc_cov[0,0],4))
assert abs(cov[0,1]-mc_cov[0,1])<3e-3

# 3. joint fixture allocation vs independent per-key allocation
rep = m.allocate(fps,[Q(9,i,1.5,price,None) for i in range(1,5)],now=NOW,exposure=ExposureBook({}))
print("hardened stakes:", [d.stake for d in rep.directives], "total", sum(d.stake for d in rep.directives))
pp={k:PropProjection(9,k[1],"tackles",*QuantEngine(game_state_k=6.0).predictive_gamma(post,1.0),st) for k in fps}
old=InstitutionalAssetManager(1e6).allocate(pp,[Q(9,i,1.5,price,None) for i in range(1,5)],now=NOW)
print("current stakes: ", [d.stake for d in old.directives], "total", sum(d.stake for d in old.directives))

# 4. exposure stacking stops
book={}; 
for cycle in range(3):
    r=m.allocate(fps,[Q(9,i,1.5,price,None) for i in range(1,5)],now=NOW,exposure=ExposureBook(dict(book)))
    for d in r.directives: book[(d.fixture_id,d.player_id,d.market_type)]=book.get((d.fixture_id,d.player_id,d.market_type),0)+d.stake
    print(f"cycle {cycle}: issued {sum(d.stake for d in r.directives):,.0f}, fixture total {sum(book.values()):,.0f}")
assert sum(book.values()) <= 0.05*1e6 + 1

# 5. future-dated quote isolated
r=m.allocate(fps,[Q(9,1,1.5,price,None),Q(9,2,1.5,price,None,at=NOW+timedelta(seconds=2))],now=NOW,exposure=ExposureBook({}))
print("future quotes:",r.future_quotes,"directives:",[(d.player_id,d.stake) for d in r.directives])
assert r.future_quotes==1 and len(r.directives)==1

# 6. depth: tiny best-price book no longer hides deep book
r=m.allocate({(9,1,"tackles"):fps[(9,1,"tackles")]},[Q(9,1,1.5,1.72,None,ms=5,book="Tiny"),Q(9,1,1.5,1.71,None,ms=50_000,book="Deep")],now=NOW,exposure=ExposureBook({}))
print("depth:",[(d.sportsbook,d.stake,d.min_acceptable_odds,d.expires_at.isoformat()) for d in r.directives])
assert {d.sportsbook for d in r.directives}=={"Tiny","Deep"}

# 7. model-risk floor kills $20k-to-win-$1k under at 1.05
low = FactorProjection(7,7,"tackles",GammaPosterior(14.4,46.0),6.0,st)
for floor in (0.0, 0.03):
    r=HardenedAssetManager(1e6, model_risk_loss_floor=floor).allocate({(7,7,"tackles"):low},[Q(7,7,2.5,8.0,1.05)],now=NOW,exposure=ExposureBook({}))
    print(f"floor={floor}: ", [(d.side,d.line_value,d.stake) for d in r.directives])

# 8. multiplier uncertainty folding
p2 = fold_multiplier_uncertainty(GammaPosterior(30.0,25.0), 0.15, 0.25)
print("card rate posterior alpha 30 -> folded shape", round(p2.alpha,3), "mean preserved", math.isclose(p2.alpha/p2.beta, 0.15*30/25))
print("ALL OK")
