# Tender portal probe

- **1** portals returned matching tenders
- **20** reachable but nothing matched (may need a search POST, or genuinely has no solar work listed)
- **3** unreachable

## Working

| State | Portal | Links | Matched | Example |
|---|---|---|---|---|
| Manipur | tender page | 78 | 3 | Electrification of tribal household in the state of Manipur through of… |

## Reachable, nothing matched

| State | Portal | Links found | Final URL |
|---|---|---|---|
| Bihar | tender page | 1 | https://bsphcl.co.in/tenders.html |
| Jharkhand | e-tender portal | 0 | https://jharkhandtenders.gov.in/ |
| Jharkhand | tender page | 34 | https://jharkhandtenders.gov.in/nicgep/app?page=Home&service=page |
| Odisha | e-tender portal | 0 | https://tendersodisha.gov.in/ |
| Assam | e-tender portal | 0 | https://assamtenders.gov.in/ |
| West Bengal | e-tender portal | 0 | https://wbtenders.gov.in/ |
| Andhra Pradesh | e-tender portal | 0 | https://tender.apeprocurement.gov.in/ |
| Telangana | e-tender portal | 0 | https://tender.telangana.gov.in/ |
| Uttar Pradesh | e-tender portal | 0 | https://etender.up.nic.in/ |
| Chhattisgarh | e-tender portal | 1 | https://eproc.cgstate.gov.in/ |
| Chhattisgarh | tender page | 0 | https://cspdcl.co.in/cseb/frmViewTenderesNEW.aspx?paramflag=2 |
| Madhya Pradesh | e-tender portal | 0 | https://mptenders.gov.in/ |
| Arunachal Pradesh | e-tender portal | 0 | https://arunachaltenders.gov.in/ |
| Arunachal Pradesh | tender page | 22 | https://power.arunachal.gov.in/tender.php |
| Meghalaya | e-tender portal | 0 | https://meghalayatenders.gov.in/ |
| Manipur | e-tender portal | 0 | https://manipurtenders.gov.in/ |
| Mizoram | e-tender portal | 0 | https://mizoramtenders.gov.in/ |
| Nagaland | e-tender portal | 0 | https://nagalandtenders.gov.in/ |
| Tripura | e-tender portal | 0 | https://tripuratenders.gov.in/ |
| Bhutan | tender page | 52 | https://www.bpc.bt/ |

## Unreachable

| State | Portal | Error | URL |
|---|---|---|---|
| Bihar | e-tender portal | ConnectionError | https://www.eproc2.bihar.gov.in/ |
| Sikkim | e-tender portal | ConnectionError | https://sikkimtenders.gov.in/ |
| Bhutan | e-tender portal | ConnectionError | https://www.gov.bt/ |

## Next step

For portals in the middle table, open the saved HTML in `portal_html/`.
A high link count with zero matches usually means the landing page is a
menu and the listing sits behind a search form, which needs a portal
specific adapter.
