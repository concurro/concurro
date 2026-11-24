//! Pure checks for values that PostgreSQL JSONB can store without changing them.

use serde_json::Value;

/// Check values that serde_json accepts but PostgreSQL jsonb cannot store.
pub fn jsonb_representable(value: &Value) -> bool {
    match value {
        Value::String(text) => !text.contains('\0'),
        Value::Array(values) => values.iter().all(jsonb_representable),
        Value::Object(fields) => fields
            .iter()
            .all(|(key, value)| !key.contains('\0') && jsonb_representable(value)),
        Value::Number(number) => {
            let raw = number.to_string();
            let (mantissa, exponent) = raw
                .split_once(['e', 'E'])
                .map_or((raw.as_str(), "0"), |(left, right)| (left, right));
            let Ok(exponent) = exponent.parse::<i64>() else {
                return false;
            };
            let mantissa = mantissa.trim_start_matches('-');
            let decimal = mantissa.find('.').unwrap_or(mantissa.len());
            let digits: Vec<u8> = mantissa.bytes().filter(u8::is_ascii_digit).collect();
            let Some(first_nonzero) = digits.iter().position(|digit| *digit != b'0') else {
                return (-16_383..=131_071).contains(&exponent);
            };
            let (Ok(decimal), Ok(count), Ok(first_nonzero)) = (
                i64::try_from(decimal),
                i64::try_from(digits.len()),
                i64::try_from(first_nonzero),
            ) else {
                return false;
            };
            let Some(point) = decimal.checked_add(exponent) else {
                return false;
            };
            point.saturating_sub(first_nonzero) <= 131_072 && count.saturating_sub(point) <= 16_383
        }
        _ => true,
    }
}
