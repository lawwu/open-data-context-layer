select ticket_id, account_id, cast(opened_at as date) as opened_on
from {{ source('bronze', 'tickets') }}
