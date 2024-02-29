"""
Reference:
  - https://srush.github.io/annotated-s4/
  - https://github.com/johnma2006/mamba-minimal/blob/master/model.py
"""
import torch
import torch.nn as nn
import torch.nn.functional as F

from einops import repeat, rearrange


def selective_scan(u, delta, A, B, C, D):
    """SSM selective scan algorithm.

    x(t+1) = Ax(t) + Bu(t)
    y(t)   = Cx(t) + Du(t)
    B, C and step size delta (used for discretization) are dependent on the input x(t).

    Glossary:
      b: batch size
      l: sequence length
      d: input dim
      n: latent state dim

    Args:
      u: [b,l,d]
      delta: [b,l,d]
      A: [d,n]
      B: [b,l,n]
      C: [b,l,n]
      D: [d,]

    Returns:
      y: [b,l,d]
    """
    b, l, d = u.shape
    n = A.size(1)

    # Discretize.
    dA = torch.einsum("bld,dn->bldn", [delta, A])           # [b,l,d,n]
    dBu = torch.einsum("bld,bln,bld->bldn", [delta, B, u])  # [b,l,d,n]

    # Selective scan.
    x = torch.zeros((b, d, n), device=u.device)
    ys = []
    for i in range(l):
        x = dA[:, i] * x + dBu[:, i]
        y = torch.einsum("bdn,bn->bd", [x, C[:, i]])
        ys.append(y)
    y = torch.stack(ys, dim=1)  # [b,l,d]
    y = y + u * D
    return y


class MambaBlock(nn.Module):
    def __init__(self, d_model, d_state, kernel_size):
        super().__init__()
        self.in_proj = nn.Linear(d_model, 2 * d_model)
        self.conv1d = nn.Conv1d(d_model, d_model, kernel_size, groups=d_model, padding=kernel_size-1)
        self.out_proj = nn.Linear(d_model, d_model)

        A = repeat(torch.arange(1, d_state + 1), "n -> d n", d=d_model)
        self.A_log = nn.Parameter(torch.log(A))
        self.D = nn.Parameter(torch.ones(d_model))

        # Project x to input-specific Δ, B, C
        self.x_proj = nn.Linear(d_model, 3*d_state, bias=False)
        self.dt_proj = nn.Linear(d_state, d_model)

    def ssm(self, x):
        # Compute ∆ A B C D, the state space parameters.
        #     A, D are input independent (see Mamba paper [1] Section 3.5.2 "Interpretation of A" for why A isn't selective)
        #     ∆, B, C are input-dependent (this is a key difference between Mamba and the linear time invariant S4,
        #                                  and is why Mamba is called **selective** state spaces)
        A = -self.A_log.exp()  # [d,n]
        D = self.D

        n = A.size(1)
        x_proj = self.x_proj(x)  # [b,l,3n]
        delta, B, C = x_proj.split([n, n, n], dim=-1)  # [b,l,n], [b,l,n], [b,l,n]
        delta = F.softplus(self.dt_proj(delta))  # [b,l,n] -> [b,l,d]
        y = selective_scan(x, delta, A, B, C, D)
        return y

    def forward(self, x):
        """
        Args:
          x: [b,l,d]

        Returns:
          y: [b,l,d]
        """
        b, l, d = x.shape
        proj = self.in_proj(x)  # [b,l,2d]
        x, res = proj.split([d, d], dim=-1)  # [b,l,d], [b,l,d]

        x = rearrange(x, "b l d -> b d l")
        x = self.conv1d(x)[:, :, :l]
        x = rearrange(x, "b d l -> b l d")
        x = F.silu(x)

        y = self.ssm(x)
        y = y + F.silu(res)
        y = self.out_proj(y)
        return y


def test_selective_scan():
    N, L, D, n = 2, 3, 16, 8
    u = torch.randn(N, L, D)
    delta = torch.randn(N, L, D)
    A = torch.randn(D, n)
    B = torch.randn(N, L, n)
    C = torch.randn(N, L, n)
    D = torch.randn(D)
    selective_scan(u, delta, A, B, C, D)


if __name__ == "__main__":
    D = 128
    x = torch.randn(2, 3, D)
    m = MambaBlock(D, D, 3)
    m(x)
