import torch
from torch import nn
from torch.utils.checkpoint import checkpoint
import math
from typing import NamedTuple


class InvariantFFHist(nn.Module):
    def __init__(
        self,
        embedding_dim,
        hidden_dim,
        problem,
        opts,
        tanh_clipping=None,
        mask_inner=None,
        mask_logits=None,
        n_encode_layers=None,
        normalization="batch",
        checkpoint_encoder=False,
        shrink_size=None,
        num_actions=4,
        n_heads=None,
        encoder=None,
    ):
        super(InvariantFFHist, self).__init__()

        self.embedding_dim = embedding_dim
        self.decode_type = None
        self.problem = problem
        self.ff = nn.Sequential(
            nn.Linear(13, 100),
            nn.ReLU(),
            nn.Linear(100, 100),
            nn.ReLU(),
            nn.Linear(100, 100),
            nn.ReLU(),
            nn.Linear(100, 1),
        )

    def forward(self, x, opts, optimizer, baseline, return_pi=False):

        _log_p, pi, cost = self._inner(x, opts)

        ll, e = self._calc_log_likelihood(_log_p, pi, None)
        if return_pi:
            return -cost, ll, pi, e

        return -cost, ll, e

    def _calc_log_likelihood(self, _log_p, a, mask):

        # Get log_p corresponding to selected actions
        entropy = -(_log_p * _log_p.exp()).sum(2).sum(1).mean()
        log_p = _log_p.gather(2, a.unsqueeze(-1)).squeeze(-1)

        # Optional: mask out actions irrelevant to objective so they do not get reinforced
        if mask is not None:
            log_p[mask] = 0
        if not (log_p > -10000).data.all():
            print(log_p)
        assert (
            log_p > -10000
        ).data.all(), "Logprobs should not be -inf, check sampling procedure!"

        # Calculate log_likelihood
        return log_p.sum(1), entropy

    def _inner(self, input, opts):

        outputs = []
        sequences = []
        state = self.problem.make_state(input, opts.u_size, opts.v_size, opts)

        # step_context = 0
        # batch_size = state.ids.size(0)
        # Perform decoding steps
        i = 1
        # entropy = 0
        while not (state.all_finished()):
            # step_size = (state.i.item() - state.u_size.item() + 1) * (
            #    state.u_size.item() + 1
            # )
            # step_size = state.i.item() + 1
            # v = state.i - (state.u_size + 1)
            # su = (state.weights[:, v, :]).float().sum(1)
            w = (state.adj[:, 0, :]).float()
            mean_w = w.mean(1)[:, None, None].repeat(1, state.u_size + 1, 1)
            mask = state.get_mask()
            s = w.reshape(state.batch_size, state.u_size + 1, 1)
            h_mean = state.hist_sum / i
            h_var = (state.hist_sum_sq - ((state.hist_sum ** 2) / i)) / i
            h_mean_degree = state.hist_deg / i
            h_mean[:, :, 0], h_var[:, :, 0], h_mean_degree[:, :, 0] = -1.0, -1.0, -1.0
            idx = (
                torch.ones(state.batch_size, 1, 1, device=opts.device)
                * i
                / state.v_size
            )
            curr_sol_size = i - state.num_skip
            var_sol = (
                state.sum_sol_sq - ((state.size ** 2) / curr_sol_size)
            ) / curr_sol_size
            mean_sol = state.size / curr_sol_size
            s = torch.cat(
                (
                    s,
                    state.matched_nodes.reshape(-1, state.u_size + 1, 1),
                    mean_w,
                    h_mean.transpose(1, 2),
                    h_var.transpose(1, 2),
                    h_mean_degree.transpose(1, 2),
                    idx.repeat(1, state.u_size + 1, 1),
                    state.size.unsqueeze(2).repeat(1, state.u_size + 1, 1)
                    / state.u_size,
                    mean_sol.unsqueeze(2).repeat(1, state.u_size + 1, 1),
                    var_sol.unsqueeze(2).repeat(1, state.u_size + 1, 1),
                    state.num_skip.unsqueeze(2).repeat(1, state.u_size + 1, 1) / i,
                    state.max_sol.unsqueeze(2).repeat(1, state.u_size + 1, 1),
                    state.min_sol.unsqueeze(2).repeat(1, state.u_size + 1, 1),
                ),
                dim=2,
            )
            # print(s)
            pi = self.ff(s).reshape(state.batch_size, state.u_size + 1)
            # Select the indices of the next nodes in the sequences, result (batch_size) long
            selected, p = self._select_node(
                pi, mask.bool()
            )  # Squeeze out steps dimension
            # entropy += torch.sum(p * (p.log()), dim=1)
            state = state.update((selected)[:, None])
            outputs.append(p)
            sequences.append(selected)
            i += 1
        # Collected lists, return Tensor
        return (
            torch.stack(outputs, 1),
            torch.stack(sequences, 1),
            state.size,
        )

    def _select_node(self, probs, mask):
        assert (probs == probs).all(), "Probs should not contain any nans"
        probs[mask] = -1e6
        p = torch.log_softmax(probs, dim=1)
        # print(p)
        if self.decode_type == "greedy":
            _, selected = p.max(1)
            # assert not mask.gather(
            #     1, selected.unsqueeze(-1)
            # ).data.any(), "Decode greedy: infeasible action has maximum probability"

        elif self.decode_type == "sampling":
            selected = p.exp().multinomial(1).squeeze(1)
            # Check if sampling went OK, can go wrong due to bug on GPU
            # See https://discuss.pytorch.org/t/bad-behavior-of-multinomial-function/10232
            # while mask.gather(1, selected.unsqueeze(-1)).data.any():
            #     print("Sampled bad values, resampling!")
            #     selected = probs.multinomial(1).squeeze(1)

        else:
            assert False, "Unknown decode type"
        return selected, p

    def set_decode_type(self, decode_type, temp=None):
        self.decode_type = decode_type
        if temp is not None:  # Do not change temperature if not provided
            self.temp = temp
