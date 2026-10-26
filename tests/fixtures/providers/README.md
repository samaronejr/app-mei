# Synthetic provider fixtures

These fixtures preserve documented response shapes, not provider qualification.
All taxpayer identifiers, names and document keys were replaced with synthetic
values. No provider account, certificate or credential is required.

SERPRO envelope: the official Integra Contador quick-start response example,
retrieved on 2026-10-22. The PAGTOWEB payload is a synthetic empty page, not a
recording of a provider call.

Focus received NF-e list: `consultar_nfes_recebidas` OpenAPI example, retrieved on
2026-10-22. The XML is deliberately minimal synthetic input.

Pinned wire routes (official Focus reference, retrieved on 2026-10-23):
`/v2/nfes_recebidas/{chave}/carta_correcao.xml`,
`/v2/nfes_recebidas/{chave}.pdf`, and
`/v2/nfsens_recebidas/{chave}.pdf`.
The PDF example names `focusnfe.s3.sa-east-1.amazonaws.com`; only that exact
storage host is trusted. Other hosts require explicit qualification and review.
