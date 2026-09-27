# Shared AI platform comparison

Reviewed 21 September 2026. Following this comparison, the user selected OpenRouter Standard for v1 and task-based model selection. A $10 USD monthly model-usage cap is selected. At the cap, paid AI pauses and offers an explicitly approved increase for the current month. The user permits reviewed non-training endpoints with disclosed temporary retention. Exact models remain pending. Recheck pricing, endpoint capabilities, and policies before implementation.

The bot and authoritative database stay on the self-hosted VM. These services provide remote model access. Select models/endpoints that support the required image inputs and structured responses; capability is not guaranteed for every model in an aggregator's catalog.

| Platform | Cost structure | Main advantage for this bot | Main tradeoff |
|---|---|---|---|
| OpenRouter Standard | Published 5.5% platform fee; model usage charged separately | Broad model choice and configurable provider/data-policy routing | Adds an intermediary; policies must cover upstream providers and fallback routes |
| Vercel AI Gateway | No token markup; payment processing and optional feature charges may apply | Competitive usage pricing and a unified API | Gateway-enforced zero data retention is currently restricted to Pro/Enterprise |
| Cloudflare AI Gateway, Unified Billing | Provider inference prices plus 5% on purchased credits | Single billing account and gateway controls | More configuration; privacy behavior requires particular care |

Sources: [OpenRouter pricing](https://openrouter.ai/pricing), [Vercel pricing](https://vercel.com/docs/ai-gateway/pricing), [Cloudflare Unified Billing](https://developers.cloudflare.com/ai-gateway/features/unified-billing/).

OpenRouter exposes provider restrictions and zero-data-retention routing. Proposed implementation should allow only approved endpoints, require supported response parameters, and validate responses locally. See [provider routing](https://openrouter.ai/docs/guides/routing/provider-selection).

Cloudflare documents ZDR support for OpenAI and Anthropic under Unified Billing. For unsupported providers, enabling ZDR falls back to standard non-ZDR billing configuration. Gateway request/response logging is configured separately. If selected, explicitly restrict eligible providers and disable content logging rather than relying on the ZDR switch alone.

Recommendation: OpenRouter Standard for v1, retaining a small replaceable adapter. Model quality on actual meal text, food photos, and nutrition labels should drive model selection. Aggregator choice alone does not establish accuracy. Vercel is a credible alternative if its plan/privacy tradeoff is acceptable; Cloudflare is most attractive when consolidating on that platform is itself a priority.

Independently of platform: preserve local/manual logging during outages, keep nutrition calculations local, send only task-relevant context, enforce an application spending budget, and retain the explicit approval requirement for rough portions. The selected privacy policy permits reviewed temporary retention but prohibits training on submitted data; the selected monthly model-usage cap is $10 USD.
