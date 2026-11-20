with tickets as (
    select account_id, count(*) as n_tickets
    from {{ ref('stg_tickets') }}
    where opened_on >= date '2026-04-01'
    group by account_id
)
select
    a.account_id,
    date_diff('month', a.opened_on, date '2026-06-30') as tenure_months,
    case when a.plan_code in ('ent', 'enterprise') then 'enterprise' when a.plan_code = 'team' then 'team' else 'free' end as plan_tier,
    coalesce(t.n_tickets, 0) as support_tickets_90d,
    case when a.cancelled_on between date '2026-07-01' and date '2026-09-28' then 1 else 0 end as churned_90d
from {{ ref('stg_accounts') }} a
left join tickets t using (account_id)
