from bloodyAD import utils
from bloodyAD.utils import LOG
from typing import Literal
import ldap3
from ldap3.core.exceptions import LDAPNoSuchObjectResult


def children(conn, target: str, type: str = "*"):
    """
    Lists children for a given target object

    :param target: sAMAccountName, DN, GUID or SID of the target
    :param type: objectClass of object to fetch: user, computer, group, organizationalUnit, container, groupPolicyContainer, etc
    """
    return conn.ldap.bloodysearch(
        target, f"(objectClass={type})", search_scope=ldap3.SUBTREE, attr=""
    )


# TODO: Fetch records from Global Catalog and also other partitions stored on other DC if possible
def dnsDump(conn, zone: str = None, detail: bool = False):
    """
    Retrieves DNS records of the Active Directory readable by the user

    :param zone: if set, prints only records in this zone
    :param detail: if set includes system records such as _ldap, _kerberos...
    """
    entries = None
    filter = "(|(objectClass=dnsNode)(objectClass=dnsZone))"

    if not detail:
        prefix_filter = ""
        for prefix in [
            "gc",
            "_gc.*",
            "_kerberos.*",
            "_kpasswd.*",
            "_ldap.*",
            "_msdcs",
            "@",
            "DomainDnsZones",
            "ForestDnsZones",
        ]:
            prefix_filter += f"(!(name={prefix}))"
        filter = f"(&{filter}{prefix_filter})"

    for nc in conn.ldap.appNCs + [conn.ldap.domainNC]:
        try:
            entries = conn.ldap.bloodysearch(
                nc,
                filter,
                search_scope=ldap3.SUBTREE,
                attr=["dnsRecord", "name", "objectClass"],
            )
        except LDAPNoSuchObjectResult:
            continue

        dnsZones = []
        for entry in entries:
            domain_suffix = entry["distinguishedName"].split(",")[1]
            domain_suffix = domain_suffix.split("=")[1]

            # RootDNSServers and ..TrustAnchors are system records not interesting for offensive normally
            if domain_suffix == "RootDNSServers" or domain_suffix == "..TrustAnchors":
                continue

            if zone and zone not in domain_suffix:
                continue

            # We keep dnsZone to list their children later
            # Useful if we have list_child on it but no read_prop on the child record
            if "dnsZone" in entry["objectClass"]:
                dnsZones.append(entry["distinguishedName"])
                continue

            domain_name = entry["name"]

            if domain_name == "@":  # @ is for dnsZone info
                domain_name = domain_suffix
            else:  # even for reverse lookup (X.X.X.X.in-addr.arpa), domain suffix should be parent name?
                domain_name = domain_name + "." + domain_suffix

            ip_addr = domain_name.split(".in-addr.arpa")
            if len(ip_addr) > 1:
                decimals = ip_addr[0].split(".")
                decimals.reverse()
                domain_name = ".".join(decimals)

            yield_entry = {"recordName": domain_name}
            for record in entry["dnsRecord"]:
                try:
                    if record["Type"] not in yield_entry:
                        yield_entry[record["Type"]] = []
                    if record["Type"] in ["A", "AAAA", "NS", "CNAME", "PTR", "TXT"]:
                        yield_entry[record["Type"]].append(record["Data"])
                    elif record["Type"] == "MX":
                        yield_entry[record["Type"]].append(record["Data"]["Name"])
                    elif record["Type"] == "SRV":
                        yield_entry[record["Type"]].append(
                            f"{record['Data']['Target']}:{record['Data']['Port']}"
                        )
                    elif record["Type"] == "SOA":
                        yield_entry[record["Type"]].append(
                            {
                                "PrimaryServer": record["Data"]["PrimaryServer"],
                                "zoneAdminEmail": record["Data"][
                                    "zoneAdminEmail"
                                ].replace(".", "@", 1),
                            }
                        )
                except KeyError:
                    LOG.error("[-] KeyError for record: " + record)
                    continue
            yield yield_entry

        # List record names if we have list child right on dnsZone but no READ_PROP on record object
        for dnsZone in dnsZones:
            try:
                entries = conn.ldap.bloodysearch(
                    dnsZone,
                    f"(objectClass=*)",
                    search_scope=ldap3.SUBTREE,
                    attr="objectClass",
                )
            except LDAPNoSuchObjectResult:
                continue
            for entry in entries:
                if entry["objectClass"]:
                    continue

                domain_parts = entry["distinguishedName"].split(",")
                domain_suffix = domain_parts[1].split("=")[1]
                domain_prefix = domain_parts[0].split("=")[1]
                domain_name = f"{domain_prefix}.{domain_suffix}"

                ip_addr = domain_name.split(".in-addr.arpa")
                if len(ip_addr) > 1:
                    decimals = ip_addr[0].split(".")
                    decimals.reverse()
                    domain_name = ".".join(decimals)

                yield {"recordName": domain_name, "type": "ACCESS DENIED"}


def membership(conn, target: str, no_recurse: bool = False):
    """
    Retrieves SID and SAM Account Names of all groups a target belongs to

    :param target: sAMAccountName, DN, GUID or SID of the target
    :param no_recurse: if set, doesn't retrieve groups where target isn't a direct member
    """
    filter = ""
    if no_recurse:
        entries = conn.ldap.bloodysearch(target, attr=["objectSid", "memberOf"])
        for entry in entries:
            for group in entry["memberOf"]:
                filter += f"(distinguishedName={group})"
        if not filter:
            LOG.warning("[!] No direct group membership found")
            return []
    else:
        # [MS-ADTS] 3.1.1.4.5.19 tokenGroups, tokenGroupsNoGCAcceptable
        attr = "tokenGroups"
        entries = conn.ldap.bloodysearch(target, attr=[attr])
        for entry in entries:
            for groupSID in entry[attr]:
                filter += f"(objectSID={groupSID})"
        if not filter:
            LOG.warning("no GC Server available, the set of groups might be incomplete")
            attr = "tokenGroupsNoGCAcceptable"
            entries = conn.ldap.bloodysearch(target, attr=[attr])
            for entry in entries:
                for groupSID in entry[attr]:
                    filter += f"(objectSID={groupSID})"

    entries = conn.ldap.bloodysearch(
        conn.ldap.domainNC,
        f"(|{filter})",
        search_scope=ldap3.SUBTREE,
        attr=["objectSID", "sAMAccountName"],
    )
    return entries


def object(
    conn, target: str, attr: str = "*", resolve_sd: bool = False, raw: bool = False
):
    """
    Retrieves LDAP attributes for the target object provided, binary data will be outputed in base64

    :param target: sAMAccountName, DN, GUID or SID of the target
    :param attr: name of the attribute to retrieve, retrieves all the attributes by default
    :param resolve_sd: if set, permissions linked to a security descriptor will be resolved (see documentation/accesscontrol.md for more information)
    :param raw: if set, will return attributes as sent by the server without any formatting, binary data will be outputed in base64
    """
    entries = conn.ldap.bloodysearch(target, attr=attr, raw=raw)
    rendered_entries = utils.renderSearchResult(entries)
    if resolve_sd and not raw:
        for entry in rendered_entries:
            if "nTSecurityDescriptor" in entry:
                entry["nTSecurityDescriptor"] = utils.renderSD(
                    entry["nTSecurityDescriptor"], conn
                )
            yield entry
    else:
        yield from rendered_entries


def search(
    conn,
    searchbase: str,
    filter: str = "(objectClass=*)",
    attr: str = "*",
    resolve_sd: bool = False,
    raw: bool = False,
):
    """
    Searches in LDAP database, binary data will be outputed in base64

    :param searchbase: DN of the parent object
    :param filter: filter to apply to the LDAP search (see Microsoft LDAP filter syntax)
    :param attr: attributes to retrieve separated by a comma
    :param resolve_sd: if set, permissions linked to a security descriptor will be resolved (see documentation/accesscontrol.md for more information)
    :param raw: if set, will return attributes as sent by the server without any formatting, binary data will be outputed in base64
    """
    entries = conn.ldap.bloodysearch(
        searchbase,
        filter,
        search_scope=ldap3.SUBTREE,
        attr=attr.split(","),
        raw=raw,
        generator=True,
    )
    rendered_entries = utils.renderSearchResult(entries)
    if resolve_sd and not raw:
        for entry in rendered_entries:
            if "nTSecurityDescriptor" in entry:
                entry["nTSecurityDescriptor"] = utils.renderSD(
                    entry["nTSecurityDescriptor"], conn
                )
            yield entry
    else:
        yield from rendered_entries


# TODO: Search writable for application partitions too?
def writable(
    conn,
    otype: Literal["ALL", "OU", "USER", "COMPUTER", "GROUP", "DOMAIN", "GPO"] = "ALL",
    right: Literal["ALL", "WRITE", "CHILD"] = "ALL",
    detail: bool = False,
    # partition: Literal["DOMAIN", "DNS", "ALL"] = "DOMAIN"
):
    """
    Retrieves objects writable by client

    :param otype: type of writable object to retrieve
    :param right: type of right to search
    :param detail: if set, displays attributes/object types you can write/create for the object
    """
    #:param partition: directory partition a.k.a naming context to explore

    if otype == "ALL":
        objectClass = "*"
    elif otype == "OU":
        objectClass = "container"
    elif otype == "GPO":
        objectClass = "groupPolicyContainer"
    else:
        objectClass = otype

    attr_params = {}
    genericReturn = (
        (lambda a: [b for b in a])
        if detail
        else (lambda a: ["permission"] if a else [])
    )
    if right == "WRITE" or right == "ALL":
        attr_params["allowedAttributesEffective"] = {
            "lambda": genericReturn,
            "right": "WRITE",
        }

        def testSDRights(a):  # Mask defined in MS-ADTS for allowedAttributesEffective
            r = []
            if not a:
                return r
            if a & 3:
                r.append("OWNER")
            if a & 4:
                r.append("DACL")
            if a & 8:
                r.append("SACL")
            return r

        attr_params["sDRightsEffective"] = {"lambda": testSDRights, "right": "WRITE"}
    if right == "CHILD" or right == "ALL":
        attr_params["allowedChildClassesEffective"] = {
            "lambda": genericReturn,
            "right": "CREATE_CHILD",
        }

    searchbases = []
    # if partition == "DOMAIN":
    searchbases.append(conn.ldap.domainNC)
    # elif partition == "DNS":
    #     searchbases.append(conn.ldap.applicationNCs) # A definir https://learn.microsoft.com/en-us/windows/win32/ad/enumerating-application-directory-partitions-in-a-forest
    # else:
    #     searchbases.append(conn.ldap.NCs) # A definir
    right_entry = {}
    for searchbase in searchbases:
        for entry in conn.ldap.bloodysearch(
            searchbase,
            f"(objectClass={objectClass})",
            search_scope=ldap3.SUBTREE,
            attr=attr_params.keys(),
            generator=True,
        ):
            for attr_name in entry:
                if attr_name not in attr_params:
                    continue
                key_names = attr_params[attr_name]["lambda"](entry[attr_name])
                for name in key_names:
                    if name == "distinguishedName":
                        name = "dn"
                    if name not in right_entry:
                        right_entry[name] = []
                    right_entry[name].append(attr_params[attr_name]["right"])

            if right_entry:
                yield {
                    **{"distinguishedName": entry["distinguishedName"]},
                    **right_entry,
                }
                right_entry = {}
