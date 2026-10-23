// SPDX-License-Identifier: GPL-2.0
/*
 * TCP QAIMD: Queue-Aware AIMD with a BDP Anchor
 * CS 422 Assignment 3 - Option 1 Kernel Module Implementation
 * Target Kernel: Linux 6.8.x (Ubuntu 24.04)
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/init.h>
#include <linux/jiffies.h>
#include <linux/math64.h>
#include <net/tcp.h>

#define QAIMD_SCALE 1000U
#define QAIMD_BETA 700U            /* 0.700 multiplicative decrease */
#define QAIMD_LOSS_TOL_NUM 20U     /* 2.0% loss tolerance (20 / 1000) */
#define QAIMD_MIN_CWND 4U          /* Floor of 4 MSS */
#define QAIMD_PROBE_RTT_MS 10000U  /* 10s min_rtt window */
#define QAIMD_PACE_GAIN 110U       /* 1.1x pacing gain (110 / 100) */

enum qaimd_state {
	QAIMD_STARTUP = 0,
	QAIMD_AVOID   = 1,
};

/* Per-socket private state (must fit in ICSK_CA_PRIV_SIZE = 104 bytes on 6.8) */
struct qaimd {
	u32 min_rtt_us;          /* Windowed min RTT (us) over 10s */
	u32 min_rtt_stamp;       /* jiffies timestamp of min_rtt_us */
	u32 rtt_round_min_us;    /* Smallest RTT seen in current round (us) */
	u32 round_Delivered;     /* tp->delivered at start of current round */

	u64 btl_bw;              /* Peak delivery rate (bytes/sec) */
	u64 prev_round_bw;       /* For startup plateau check */

	u32 round_retrans_start; /* tp->total_retrans at start of round */
	u32 last_bbr_bdp;        /* Last computed BDP in segments (for RTO cap) */

	u32 probe_rtt_done_stamp;/* jiffies when PROBE_RTT drain finishes */
	u16 cwnd_prior;          /* cwnd saved before PROBE_RTT */

	u8  state:2,             /* QAIMD_STARTUP or QAIMD_AVOID */
	    in_probe_rtt:1,      /* Currently draining queue for min_rtt */
	    comp_mode:1,         /* Competitive mode (coexisting with CUBIC) */
	    hi_q_rounds:4;       /* Consecutive rounds with q > 2*q_thr */

	u8  plateau_rounds;      /* Consecutive rounds with <25% bw growth */
	u8  comp_rounds;         /* Consecutive rounds with q > q_thr & no loss */
	u32 prev_round_cwnd;     /* cwnd at previous round boundary */
};

static u32 qaimd_bdp_segs(struct sock *sk, const struct qaimd *ca)
{
	struct tcp_sock *tp = tcp_sk(sk);
	u32 mss = max_t(u32, tp->mss_cache, 1U);
	u64 bdp_bytes;

	if (ca->min_rtt_us == ~0U || ca->btl_bw == 0)
		return tcp_snd_cwnd(tp);

	/* bdp_bytes = btl_bw (B/s) * min_rtt_us (us) / 1e6 */
	bdp_bytes = div_u64(ca->btl_bw * (u64)ca->min_rtt_us, USEC_PER_SEC);
	return max_t(u32, div_u64(bdp_bytes, mss), QAIMD_MIN_CWND);
}

static u32 qaimd_q_thr_us(const struct qaimd *ca)
{
	u32 thr;

	if (ca->min_rtt_us == ~0U)
		return 3000U;
	/* clamp(0.15 * min_rtt, 3 ms, 15 ms) */
	thr = (ca->min_rtt_us * 15U) / 100U;
	return clamp(thr, 3000U, 15000U);
}

static void qaimd_set_pacing_rate(struct sock *sk)
{
	struct tcp_sock *tp = tcp_sk(sk);
	u32 srtt_us = max_t(u32, tp->srtt_us >> 3, 1000U);
	u64 rate;

	/* pacing_rate = 1.1 * cwnd * mss / srtt */
	rate = (u64)tcp_snd_cwnd(tp) * (u64)tp->mss_cache * USEC_PER_SEC;
	rate = div_u64(rate * QAIMD_PACE_GAIN, (u64)srtt_us * 100ULL);

	WRITE_ONCE(sk->sk_pacing_rate, min_t(u64, rate, sk->sk_max_pacing_rate));
}

static void qaimd_init(struct sock *sk)
{
	struct tcp_sock *tp = tcp_sk(sk);
	struct qaimd *ca = inet_csk_ca(sk);

	memset(ca, 0, sizeof(*ca));
	ca->state = QAIMD_STARTUP;
	ca->min_rtt_us = ~0U;
	ca->min_rtt_stamp = jiffies;
	ca->rtt_round_min_us = ~0U;
	ca->round_Delivered = tp->delivered;
	ca->round_retrans_start = tp->total_retrans;
	ca->prev_round_cwnd = tcp_snd_cwnd(tp);

	/* Enable kernel internal pacing if fq qdisc is not used */
	cmpxchg(&sk->sk_pacing_status, SK_PACING_NONE, SK_PACING_NEEDED);
}

static void qaimd_update_Round(struct sock *sk, const struct rate_sample *rs)
{
	struct tcp_sock *tp = tcp_sk(sk);
	struct qaimd *ca = inet_csk_ca(sk);
	u32 delivered_delta = tp->delivered - ca->round_Delivered;
	u32 retrans_delta = tp->total_retrans - ca->round_retrans_start;
	u32 srtt_us = max_t(u32, tp->srtt_us >> 3, 1000U);
	u32 q_us = 0, q_thr_us, bdp;
	bool loss_occurred, congestive_loss;
	u32 cwnd = tcp_snd_cwnd(tp);

	if (ca->rtt_round_min_us != ~0U && ca->min_rtt_us != ~0U &&
	    ca->rtt_round_min_us > ca->min_rtt_us)
		q_us = ca->rtt_round_min_us - ca->min_rtt_us;

	q_thr_us = qaimd_q_thr_us(ca);
	bdp = qaimd_bdp_segs(sk, ca);
	ca->last_bbr_bdp = bdp;

	/* Loss proxy: retrans_pkts_this_round / delivered_pkts_this_round */
	loss_occurred = (retrans_delta > 0);
	congestive_loss = loss_occurred &&
		(q_us > q_thr_us ||
		 (delivered_delta > 0 &&
		  retrans_delta * QAIMD_SCALE > delivered_delta * QAIMD_LOSS_TOL_NUM));

	/* Competitive mode check: 20 rounds of q > q_thr with shrinking cwnd & 0 loss */
	if (q_us > q_thr_us && !loss_occurred && cwnd <= ca->prev_round_cwnd) {
		if (ca->comp_rounds < 255)
			ca->comp_rounds++;
		if (ca->comp_rounds >= 20)
			ca->comp_mode = 1;
	} else if (q_us <= q_thr_us) {
		ca->comp_rounds = 0;
		ca->comp_mode = 0;
	}
	ca->prev_round_cwnd = cwnd;

	if (ca->state == QAIMD_STARTUP) {
		if (q_us > 2 * q_thr_us)
			ca->hi_q_rounds = min_t(u8, ca->hi_q_rounds + 1, 15);
		else
			ca->hi_q_rounds = 0;

		if (ca->btl_bw < (ca->prev_round_bw * 125ULL) / 100ULL)
			ca->plateau_rounds++;
		else {
			ca->plateau_rounds = 0;
			ca->prev_round_bw = ca->btl_bw;
		}

		if (congestive_loss || ca->hi_q_rounds >= 2 || ca->plateau_rounds >= 3) {
			ca->state = QAIMD_AVOID;
			cwnd = max_t(u32, bdp, QAIMD_MIN_CWND);
			tp->snd_ssthresh = cwnd;
		} else if (tcp_is_cwnd_limited(sk)) {
			cwnd = min_t(u32, cwnd * 2U, tp->snd_cwnd_clamp);
		}
	} else {
		/* QAIMD_AVOID */
		if (congestive_loss) {
			cwnd = max_t(u32, (cwnd * QAIMD_BETA) / QAIMD_SCALE, QAIMD_MIN_CWND);
			tp->snd_ssthresh = cwnd;
		} else if (!ca->comp_mode && q_us > q_thr_us) {
			/* Proportional delay backoff: cwnd * (1 - 0.5 * (q - q_thr) / srtt) */
			u32 excess_us = q_us - q_thr_us;
			u32 cut_permil = min_t(u32, (500U * excess_us) / srtt_us, 300U);
			cwnd = max_t(u32, (cwnd * (QAIMD_SCALE - cut_permil)) / QAIMD_SCALE,
				     QAIMD_MIN_CWND);
		} else if (cwnd < bdp) {
			/* Pipe not full: catch up by max(1 MSS, (bdp - cwnd) / 4) */
			u32 step = max_t(u32, 1U, (bdp - cwnd) / 4U);
			cwnd = min_t(u32, cwnd + step, tp->snd_cwnd_clamp);
		} else if (tcp_is_cwnd_limited(sk)) {
			/* Additive increase: +1 MSS per round */
			cwnd = min_t(u32, cwnd + 1U, tp->snd_cwnd_clamp);
		}
	}

	tcp_snd_cwnd_set(tp, cwnd);

	/* Advance round markers */
	ca->round_Delivered = tp->delivered;
	ca->round_retrans_start = tp->total_retrans;
	ca->rtt_round_min_us = ~0U;
}

static void qaimd_main(struct sock *sk, u32 ack, int flag, const struct rate_sample *rs)
{
	struct tcp_sock *tp = tcp_sk(sk);
	struct qaimd *ca = inet_csk_ca(sk);
	u32 rtt_us;

	/* 1. Update RTT signals */
	if (rs->rtt_us > 0) {
		rtt_us = (u32)rs->rtt_us;
		if (rtt_us < ca->rtt_round_min_us)
			ca->rtt_round_min_us = rtt_us;

		if (rtt_us <= ca->min_rtt_us ||
		    time_after32(jiffies, ca->min_rtt_stamp + msecs_to_jiffies(QAIMD_PROBE_RTT_MS))) {
			ca->min_rtt_us = rtt_us;
			ca->min_rtt_stamp = jiffies;
		}
	}

	/* 2. Update bottleneck bandwidth estimate (skip app-limited samples) */
	if (rs->delivered > 0 && rs->interval_us > 0 && !rs->is_app_limited) {
		u64 bw = div64_u64((u64)rs->delivered * (u64)tp->mss_cache * USEC_PER_SEC,
				   (u64)rs->interval_us);
		if (bw > ca->btl_bw)
			ca->btl_bw = bw;
		else
			/* Mild decay (99.5% per ACK) so stale 10-round peaks expire */
			ca->btl_bw = (ca->btl_bw * 995ULL) / 1000ULL;
	}

	/* 3. Check PROBE_RTT drain window */
	if (!ca->in_probe_rtt && ca->min_rtt_us != ~0U &&
	    time_after32(jiffies, ca->min_rtt_stamp + msecs_to_jiffies(QAIMD_PROBE_RTT_MS))) {
		u32 srtt_ms = max_t(u32, (tp->srtt_us >> 3) / 1000U, 200U);
		ca->in_probe_rtt = 1;
		ca->cwnd_prior = tcp_snd_cwnd(tp);
		ca->probe_rtt_done_stamp = jiffies + msecs_to_jiffies(srtt_ms);
		tcp_snd_cwnd_set(tp, max_t(u32, qaimd_bdp_segs(sk, ca) / 2U, QAIMD_MIN_CWND));
	}

	if (ca->in_probe_rtt) {
		tcp_snd_cwnd_set(tp, min_t(u32, tcp_snd_cwnd(tp),
					   max_t(u32, qaimd_bdp_segs(sk, ca) / 2U, QAIMD_MIN_CWND)));
		if (time_after32(jiffies, ca->probe_rtt_done_stamp)) {
			ca->in_probe_rtt = 0;
			ca->min_rtt_stamp = jiffies;
			tcp_snd_cwnd_set(tp, max_t(u32, ca->cwnd_prior, qaimd_bdp_segs(sk, ca)));
		}
		qaimd_set_pacing_rate(sk);
		return;
	}

	/* 4. Check round-trip boundary */
	if (!before(tp->snd_una, tp->snd_nxt) ||
	    (tp->delivered - ca->round_Delivered) >= max_t(u32, tcp_snd_cwnd(tp), 1U)) {
		qaimd_update_Round(sk, rs);
	}

	qaimd_set_pacing_rate(sk);
}

static u32 qaimd_ssthresh(struct sock *sk)
{
	const struct tcp_sock *tp = tcp_sk(sk);
	struct qaimd *ca = inet_csk_ca(sk);
	u32 q_us = 0;

	if (ca->rtt_round_min_us != ~0U && ca->min_rtt_us != ~0U &&
	    ca->rtt_round_min_us > ca->min_rtt_us)
		q_us = ca->rtt_round_min_us - ca->min_rtt_us;

	/* If loss happens without standing queue, keep cwnd anchored near BDP */
	if (q_us <= qaimd_q_thr_us(ca))
		return max_t(u32, tcp_snd_cwnd(tp), QAIMD_MIN_CWND);

	return max_t(u32, (tcp_snd_cwnd(tp) * QAIMD_BETA) / QAIMD_SCALE, QAIMD_MIN_CWND);
}

static u32 qaimd_undo_cwnd(struct sock *sk)
{
	return tcp_sk(sk)->snd_cwnd;
}

static void qaimd_set_state(struct sock *sk, u8 new_state)
{
	struct tcp_sock *tp = tcp_sk(sk);
	struct qaimd *ca = inet_csk_ca(sk);

	if (new_state == TCP_CA_Loss) {
		/* On RTO: cwnd = MIN_CWND; state = STARTUP capped at last bdp */
		ca->state = QAIMD_STARTUP;
		tcp_snd_cwnd_set(tp, QAIMD_MIN_CWND);
		if (ca->last_bbr_bdp > QAIMD_MIN_CWND)
			tp->snd_ssthresh = ca->last_bbr_bdp;
	}
}

static struct tcp_congestion_ops tcp_qaimd_ops __read_mostly = {
	.name		= "qaimd",
	.owner		= THIS_MODULE,
	.init		= qaimd_init,
	.ssthresh	= qaimd_ssthresh,
	.undo_cwnd	= qaimd_undo_cwnd,
	.set_state	= qaimd_set_state,
	.cong_control	= qaimd_main,
};

static int __init qaimd_register(void)
{
	BUILD_BUG_ON(sizeof(struct qaimd) > ICSK_CA_PRIV_SIZE);
	return tcp_register_congestion_control(&tcp_qaimd_ops);
}

static void __exit qaimd_unregister(void)
{
	tcp_unregister_congestion_control(&tcp_qaimd_ops);
}

module_init(qaimd_register);
module_exit(qaimd_unregister);

MODULE_AUTHOR("Kush Khanna");
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("TCP QAIMD Congestion Control (CS 422 Assignment 3)");