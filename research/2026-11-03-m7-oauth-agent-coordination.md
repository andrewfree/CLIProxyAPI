# M7 OAuth and skills for agent coordination

Captured: 2026-11-03. Research by GPT-6.1 Sol, limited to the public post and linked product documentation.

Source: [The Grim Scalper on X](https://x.com/thegrimscalper/status/2117267021029368269), published 2026-11-02 at 14:46:09 UTC according to the [FxTwitter text mirror](https://api.fxtwitter.com/thegrimscalper/status/2117267021029368269). Direct X access returned HTTP 403, so the post text and timestamp were read through that mirror.

The useful idea is to give agents skills that explain a shared work environment, then give each agent permission to use its services. The post presents M7 as an example combining identity, knowledge access, coordination, and backend controls. The public documentation supports parts of that design; the author's complete workflow remains a claim.

## What the author describes

The post replies to [a question about getting more from skills](https://x.com/fjzeit/status/2117264873533112594), whose text was also read through [FxTwitter](https://api.fxtwitter.com/fjzeit/status/2117264873533112594).

- Skills provide a starting point and a blueprint for how an agent should work.
- M7 supplies OAuth and multiple-tenant infrastructure. Agents use Siglatch for backend operations and interact with tickets, wikis, and retrieval-augmented search, which retrieves relevant knowledge for a model.
- The author says Grok, Claude, and Codex can coordinate through code checkout and locking, including at low reasoning settings. Partners connect with OAuth credentials and a knowledge-base entry point.

The post does not explain the provider connection behind the author's statement that API access is unnecessary. It does not establish a particular subscription route or unattended execution result.

## What the public documentation supports

[M7 Identity](https://m7.org/) describes OAuth 2.0 and OpenID Connect, organization boundaries, and permissions for people, applications, and agents.

The [apps and agents documentation](https://m7.org/products/platform/machine-to-machine) distinguishes access delegated by a person from service identities using client credentials. It describes scoped, expiring access, revocation, and supplying agents with API documentation or tool definitions.

The [integration quickstart](https://m7.org/docs/api/sso.user.m7.org/quickstart.md) documents authorization code flow with PKCE S256 and exact registered HTTPS redirect URIs. This is an integration contract, not proof of the post's agent workflow.

[Siglatch](https://m7.org/products/siglatch) is described as an encrypted UDP daemon that dispatches configured handlers for small control messages. That supports a backend control component; it does not establish a complete orchestration system. The page labels version 1.0 stable and version 2.0 development.

## Relevance to CLIProxy and shared agent tooling

Inference: model access and shared workspace access can be designed separately. A skill could explain the common tools, while scoped identities govern access to tickets, knowledge, and coordination services. The post is a useful architecture lead for that separation.

This research does not qualify M7 for adoption. Tenant isolation, lock behavior, retrieval quality, credential lifecycle, and the claimed cross-model workflow were not tested. No software was installed, credentials issued, or proxy configuration changed.
