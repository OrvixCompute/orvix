-- ============================================================================
-- Orvix Orchestrator — migration 014: public network stats
-- Run AFTER 001-013. Idempotent (create or replace / if not exists).
--
-- Backs GET /v1/network/stats, the public dashboard feed. The aggregation runs
-- server-side in one round trip instead of pulling every jobs row into the app.
-- Mock jobs (jobs.is_mock) are excluded — public stats reflect real compute only.
-- ============================================================================

begin;

-- ----------------------------------------------------------------------------
-- 1. Indexes for the rolling-window scans. The existing jobs index is
--    (user_id, created_at) which can't serve a network-wide time filter.
-- ----------------------------------------------------------------------------
create index if not exists idx_jobs_created_at       on jobs (created_at desc);
create index if not exists idx_image_jobs_created_at on image_jobs (created_at desc);

-- ----------------------------------------------------------------------------
-- 2. network_stats(p_window_hours) — one JSON blob of public network metrics.
--    Registered/offline node counts come from the `nodes` table; the caller
--    overlays live websocket connection counts from the in-memory registry.
-- ----------------------------------------------------------------------------
create or replace function network_stats(p_window_hours integer default 24)
returns json
language sql
stable
as $$
    select json_build_object(
        'window_hours', p_window_hours,
        'nodes', (
            select json_build_object(
                'registered',    count(*),
                'ready',         count(*) filter (where status = 'ready'),
                'busy',          count(*) filter (where status = 'busy'),
                'draining',      count(*) filter (where status = 'draining'),
                'offline',       count(*) filter (where status = 'offline'),
                'chat_capable',  count(*) filter (where 'chat'  = any(engines)),
                'image_capable', count(*) filter (where 'image' = any(engines)),
                'total_vram_gb', coalesce(sum(vram_gb), 0)
            )
            from nodes
        ),
        'gpus', coalesce((
            select json_agg(json_build_object('gpu_model', gpu_model, 'count', n)
                            order by n desc, gpu_model)
            from (
                select gpu_model, count(*) as n
                from nodes
                where gpu_model is not null and gpu_model <> ''
                group by gpu_model
            ) g
        ), '[]'::json),
        'providers', (
            select json_build_object(
                'total',  count(*) filter (where is_provider),
                'staked', count(*) filter (where is_provider and staked_orvx > 0)
            )
            from users
        ),
        'chat', (
            select json_build_object(
                'requests_total',  count(*),
                'requests_window', count(*) filter (where created_at >= now() - make_interval(hours => p_window_hours)),
                'tokens_total',    coalesce(sum(total_tokens), 0),
                'tokens_window',   coalesce(sum(total_tokens) filter (where created_at >= now() - make_interval(hours => p_window_hours)), 0),
                'avg_latency_ms',  round(avg(latency_ms) filter (where created_at >= now() - make_interval(hours => p_window_hours)))
            )
            from jobs
            where status = 'completed' and not is_mock
        ),
        'images', (
            select json_build_object(
                'generated_total',  count(*),
                'generated_window', count(*) filter (where created_at >= now() - make_interval(hours => p_window_hours))
            )
            from image_jobs
        )
    );
$$;

comment on function network_stats(integer) is
    'Public network metrics for GET /v1/network/stats. Excludes mock jobs.';

commit;
