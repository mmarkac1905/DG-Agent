{{ config(materialized='table') }}

/*
    Dimension: Date — generated calendar covering 2023-01-01 to 2027-12-31.
*/

WITH date_spine AS (
    SELECT
        CAST(UNNEST(GENERATE_SERIES(
            DATE '2023-01-01',
            DATE '2027-12-31',
            INTERVAL 1 DAY
        )) AS DATE) AS date_day
)

SELECT
    date_day AS date_key,
    date_day,
    EXTRACT(YEAR FROM date_day) AS year,
    EXTRACT(QUARTER FROM date_day) AS quarter,
    EXTRACT(MONTH FROM date_day) AS month,
    EXTRACT(DAY FROM date_day) AS day_of_month,
    EXTRACT(DOW FROM date_day) AS day_of_week,
    EXTRACT(WEEK FROM date_day) AS week_of_year,
    CASE EXTRACT(DOW FROM date_day)
        WHEN 0 THEN 'Sunday' WHEN 1 THEN 'Monday' WHEN 2 THEN 'Tuesday'
        WHEN 3 THEN 'Wednesday' WHEN 4 THEN 'Thursday' WHEN 5 THEN 'Friday'
        WHEN 6 THEN 'Saturday'
    END AS day_name,
    CASE EXTRACT(MONTH FROM date_day)
        WHEN 1 THEN 'January' WHEN 2 THEN 'February' WHEN 3 THEN 'March'
        WHEN 4 THEN 'April' WHEN 5 THEN 'May' WHEN 6 THEN 'June'
        WHEN 7 THEN 'July' WHEN 8 THEN 'August' WHEN 9 THEN 'September'
        WHEN 10 THEN 'October' WHEN 11 THEN 'November' WHEN 12 THEN 'December'
    END AS month_name,
    CAST(EXTRACT(YEAR FROM date_day) AS VARCHAR) || '-Q' || CAST(EXTRACT(QUARTER FROM date_day) AS VARCHAR) AS year_quarter,
    CAST(EXTRACT(YEAR FROM date_day) AS VARCHAR) || '-' || LPAD(CAST(EXTRACT(MONTH FROM date_day) AS VARCHAR), 2, '0') AS year_month,
    CASE WHEN EXTRACT(DOW FROM date_day) IN (0, 6) THEN FALSE ELSE TRUE END AS is_weekday,
    date_day = CURRENT_DATE AS is_today
FROM date_spine
