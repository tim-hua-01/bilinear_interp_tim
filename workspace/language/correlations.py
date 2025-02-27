# %%
%load_ext autoreload
%autoreload 2

from language import Transformer, Sight
from sae import SAE, Tracer
from datasets import load_dataset
import torch
from einops import rearrange, einsum
from plotly import express as px
from tqdm import tqdm
from safetensors.torch import save_file, load_file
import os
import pandas as pd

torch.set_grad_enabled(False)
color = dict(color_continuous_scale="RdBu", color_continuous_midpoint=0.0)
# %%
name = "fw-medium"
model = Transformer.from_pretrained(f"tdooms/{name}")
dataset = load_dataset("tdooms/fineweb-16k", split="train").with_format("torch")

layer = 12
tracer = Tracer(model, layer=layer, inp=None, out=dict(expansion=4))
# %%
def truncated_eigh(tensor, k=64):
    vals, vecs = torch.linalg.eigh(tensor)
    idxs = vals.abs().topk(k, dim=-1).indices
    
    vals = torch.gather(vals, -1, idxs)
    vecs = torch.gather(vecs, -1, idxs[:, None, :].expand(-1, tensor.size(-1), -1))
    return vals, vecs

path = f"data/cache/{name}-vecs{layer}.pt"
if os.path.exists(path):
    data = load_file(path, device="cuda")
    vals, vecs = data["vals"], data["vecs"]
else:
    vals, vecs = zip(*tracer.compute(truncated_eigh))
    vals, vecs = torch.cat(vals), torch.cat(vecs)
    save_file(dict(vals=vals, vecs=vecs), path)
# %%
sight = Sight(model)
input_ids = torch.stack([row["input_ids"] for row in dataset.take(256)])

with sight.trace(input_ids, scan=False, validate=False):
    acts = sight["mlp-in", layer][:, 1:].save()
torch.cuda.empty_cache()
# %%
mlp = model.transformer.h[layer].mlp
ys, y_hats = [], []

for i in tqdm(range(input_ids.size(0))):
    y = tracer.out.encode(mlp(acts[i])).T

    pred = einsum(acts[i], vecs, "... f, o f k -> o ... k").pow(2)
    y_hat = einsum(pred, vals, "o ... k, o k -> o ... k").cumsum(-1)
    y_hat.masked_fill_(~(y[..., None].bool()), 0.0)

    y_hat = y_hat.to_sparse()
    y = y.to_sparse()

    ys.append(y)
    y_hats.append(y_hat)

y = torch.cat(ys, dim=1)
y_hat = torch.cat(y_hats, dim=1)

del ys, y_hats
torch.cuda.empty_cache()
# %%

metrics = []
for i in tqdm(range(tracer.out.d_features)):
    tmp = torch.stack([y[i].coalesce().values(), y_hat[i].T[1].coalesce().values()])
    
    metrics.append(dict(
        corr=torch.corrcoef(tmp)[0, 1].item(),
        nnz=y[i]._nnz(),
    ))
df = pd.DataFrame(metrics)

tensor = torch.tensor(list(df[df["nnz"] > 10]["corr"]))
px.histogram(x=tensor).show()
tensor.mean().item()
# %%
# px.scatter(df, x="nnz", y="corr", log_x=True, opacity=0.2, hover_name=df.index).show()
# %%
from plotly import express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from itertools import product

titles = []
for i in range(5, 14):
    a = y[i].coalesce().values().cpu()
    b = y_hat[i].T[2].coalesce().values().cpu()
    tmp = torch.stack([a, b])
    corr = torch.corrcoef(tmp)[0, 1].item()
    titles.append(f"{corr:.2f}")

color = px.colors.qualitative.Plotly[0]
fig = make_subplots(rows=3, cols=3, subplot_titles=titles, vertical_spacing=0.09, x_title="Activation", y_title="Approximation")
for i, j in product(range(3), range(3)):
    idx = 5 + i * 3 + j
    a = y[idx].coalesce().values().cpu()
    b = y_hat[idx].T[1].coalesce().values().cpu() + tracer.out.w_enc(tracer.out.b_dec)[idx].cpu()
    fig.add_scatter(x=a, y=b, marker=dict(color=color), showlegend=False, mode="markers", row=i + 1, col=j + 1)
    fig.add_scatter(x=[0, 20], y=[0, 20], line=dict(color="gray", dash='dot', width=1), showlegend=False, mode="lines", row=i + 1, col=j + 1)

fig.update_layout(template="plotly_white")
fig.update_xaxes(showticklabels=False, range=(0, 20), showgrid=False).update_yaxes(showticklabels=False, range=(-2, 20), showgrid=False)
fig.update_layout(width=400, height=400, margin=dict(l=70, r=0, t=30, b=50), showlegend=False)
fig.write_image(f"C:\\Users\\thoma\\Downloads\\correlation_scatters.pdf", engine="kaleido")
# %%
q = []
for k in range(64):
    corrs = dict()
    for i in tqdm(range(tracer.out.d_features)):
        if y[i]._nnz() < 10:
            continue
        
        x0, x1 = y[i].coalesce().values(), y_hat[i].T[k].coalesce().values()
        if len(x0) != len(x1):
            continue
        
        tmp = torch.stack([x0, x1])
        corrs[i] = torch.corrcoef(tmp)[0, 1].item()

    tensor = torch.tensor(list(corrs.values()))
    q += [tensor.mean().item()]

px.line(q)
# %%
df.to_csv(f"data/results/{name}-metrics{layer}.csv", index=False)
pd.DataFrame(dict(corr=q)).to_csv(f"data/results/{name}-corr{layer}.csv", index=False)
# %%
