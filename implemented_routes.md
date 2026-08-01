This repository is an outbound client, not a Python web server. It implements 71 distinct REST method/path pairs against `https://api.halocollar.com`, plus OAuth and SignalR endpoints. Trailing slashes below are intentional.

## Halo REST API — 71 pairs

### Configuration, collars, and pets (19)

| HTTP | Path | Python method(s) |
|---|---|---|
| GET | `/configuration/` | `configuration()`, `videos()` |
| GET | `/collar/my` | `collars()`, `firmware_statuses()`, `collar_for_pet()` |
| GET | `/collar/{id}` | `collar()`, `firmware_status()` |
| PUT | `/collar/check-can-be-bound-to-user` | `check_collar_binding()` |
| PUT | `/collar/bind-to-user` | `bind_collar()` |
| POST | `/collar/{id}/unbind-from-user` | `unbind_collar_from_user()` |
| PUT | `/collar/{id}/find` | `find_collar()` |
| GET | `/pet/my` | `pets()` |
| GET | `/pet/{id}` | `pet()` |
| PUT | `/pet/{id}/bind-collar` | `bind_collar_to_pet()` |
| PUT | `/pet/{id}/unbind-collar` | `unbind_collar_from_pet()` |
| GET | `/pet/colors` | `pet_colors()` |
| GET | `/pet/{id}/correction-rules` | `pet_correction_rules()` |
| POST | `/pet/{id}/run-instant-correction/` | `send_instant_correction()` |
| POST | `/pet/add` | `add_pet()` |
| PUT | `/pet/{id}` | `update_pet()` |
| DELETE | `/pet/{id}` | `delete_pet()` |
| PUT | `/pet/{id}/instant-mode` | `set_pet_fences_enabled()` |
| PUT | `/pet/check-name-uniqueness` | `pet_name_is_available()` |

### Account, profile, and beacons (25)

| HTTP | Path | Python method(s) |
|---|---|---|
| GET | `/account/my/map` | `account_map()`, `geofences()`, `geo_fence_pet_sync()` |
| POST | `/account/mobile-data` | `register_mobile_device()` |
| POST | `/account/check-user-can-change-email` | `check_user_can_change_email()` |
| POST | `/account/email-change-request` | `request_email_change()` |
| POST | `/account/email-change-request/confirm` | `confirm_email_change()` |
| POST | `/account/email-change-request/resend-email` | `resend_email_change_confirmation()` |
| PUT | `/account/email-change-request` | `cancel_email_change()` |
| DELETE | `/account` | `delete_account()` |
| POST | `/account/generate-ecommerce-login-magic-code` | `generate_ecommerce_login_magic_code()` |
| GET | `/user-profile` | `user_profile()` |
| PUT | `/user-profile` | `update_profile_name()` |
| PUT | `/user-profile/me/icon` | `upload_profile_avatar()` |
| DELETE | `/user-profile/me/icon` | `delete_profile_avatar()` |
| GET | `/user-profile/onboarding/progress` | `onboarding_progress()` |
| PUT | `/user-profile/onboarding/progress` | `update_onboarding_progress()` |
| GET | `/user-profile/questionnaire` | `questionnaire()` |
| PUT | `/user-profile/questionnaire` | `save_questionnaire()` |
| GET | `/beacon/my` | `beacons()`, `beacon_pet_sync()` |
| PUT | `/beacon/check-name-uniqueness` | `beacon_name_is_available()` |
| PUT | `/beacon/check-can-be-bound-to-user` | `check_beacon_binding()` |
| POST | `/beacon` | `add_beacon()` |
| PUT | `/beacon/{id}` | `update_beacon()` |
| DELETE | `/beacon/{id}` | `delete_beacon()` |
| PUT | `/beacon/telemetry` | `upload_beacon_telemetry()` |
| PUT | `/beacon/set-is-assigned/{petId}` | `set_pet_beacons_assigned()` |

### Walks, notifications, system, corrections, and training (18)

| HTTP | Path | Python method(s) |
|---|---|---|
| GET | `/walk/my` | `walks()` |
| GET | `/walk/{id}/summary` | `walk_summary()` |
| POST | `/walk/{id}/set-is-paused` | `set_walk_paused()` |
| POST | `/walk/{id}/stop` | `stop_walk()` |
| POST | `/walk/{id}/mark-ended` | `mark_walk_ended()` |
| PUT | `/walk/{id}/trail-thumbnail` | `upload_walk_trail_thumbnail()` |
| PUT | `/walk/{id}/pet/{petId}/trail-image` | `upload_walk_pet_trail_image()` |
| GET | `/notification/my/query` | `notifications()` |
| GET | `/portal-notification/my/in-app/` | `portal_notifications()` |
| PUT | `/notification/status` | `set_notification_status()` |
| GET | `/mapbox/request/my` | `mapbox_requests()` |
| GET | `/subscription/my/` | `subscription()` |
| GET | `/system/server-date-time` | `server_time()`; automatic preflight before the first authenticated write |
| GET | `/correction-rule/configuration-v2` | `correction_rule_configuration()` |
| PUT | `/correction-rule` | `update_correction_rules()` |
| PUT | `/correction-rule/test-on-collar` | `test_correction_on_collar()` |
| GET | `/training/my-v2` | `training()` |
| GET | `/training/user/course-launch-link/{curriculum}/{course}` | `training_course_link()` |

### Fences, push notifications, and parcels (9)

| HTTP | Path | Python method(s) |
|---|---|---|
| POST | `/geo-fence/safe-zones` | `geo_fence_safe_zones()` |
| PUT | `/geo-fence/check-name-uniqueness` | `geo_fence_name_is_available()` |
| POST | `/geo-fence/add` | `add_geo_fence()` |
| PUT | `/geo-fence/{id}` | `rename_geo_fence()` |
| PUT | `/geo-fence/{id}/location` | `update_geo_fence_location()` |
| DELETE | `/geo-fence/{id}` | `delete_geo_fence()` |
| PUT | `/push-notification/subscribe` | `subscribe_push_notifications()` |
| PUT | `/push-notification/unsubscribe-device` | `unsubscribe_push_device()` |
| POST | `/report-all/api/parcels` | `lookup_parcels()` |

Route-specific query parameters implemented by the client:

- `/pet/{id}`: `RefreshTelemetry`
- `/account/my/map`: `RefreshTelemetry`, `MaxCorrectionsCount`, optionally `viewport.center.latitude` and `viewport.center.longitude`
- `/walk/my`: `page`, `pageSize`
- `/notification/my/query`: `Page`, `PageSize`

## OAuth endpoints

| HTTP | Endpoint | Python method(s) |
|---|---|---|
| GET | `https://auth.halocollar.com/connect/authorize` | `HaloOAuth.begin_login()` creates this browser URL |
| POST | `https://auth.halocollar.com/connect/token` | `complete_login()`, `password_login()`, `refresh()`, and `HaloClient.refresh_login()` |

`haloapp://callback` is a custom URI parsed locally; this package does not send an HTTP request to it.

## SignalR endpoints

| Protocol/method | Endpoint |
|---|---|
| POST | `https://halo-prod-sockets-app.azurewebsites.net/TelemetryHub/negotiate?negotiateVersion=1` |
| POST | `https://halo-prod-sockets-app.azurewebsites.net/NotificationHub/negotiate?negotiateVersion=1` |
| POST | Server-provided Azure redirect URL plus `/negotiate?negotiateVersion=1` |
| WebSocket | Final negotiated URL with `id={connectionToken}` appended |

The SignalR client supports exactly `TelemetryHub` and `NotificationHub`. There are no implemented `PATCH`, inbound server, or Dog Park API routes.
