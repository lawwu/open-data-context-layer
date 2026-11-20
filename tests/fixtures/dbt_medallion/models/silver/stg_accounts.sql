select
    account_id,
    cast(created_at as date) as opened_on,
    lower(trim(plan)) as plan_code,
    cast(cancelled_at as date) as cancelled_on
from {{ source('bronze', 'accounts') }}
