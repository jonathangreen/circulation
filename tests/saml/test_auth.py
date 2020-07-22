import urlparse
from base64 import b64encode
from xml.dom.minidom import Document

from defusedxml.lxml import fromstring
from mock import create_autospec, MagicMock
from nose.tools import eq_
from onelogin.saml2.utils import OneLogin_Saml2_Utils
from parameterized import parameterized

from api.saml.auth import SAMLAuthenticationManager, SAMLAuthenticationManagerFactory
from api.saml.configuration import SAMLOneLoginConfiguration, SAMLConfiguration, ExternalIntegrationOwner
from api.saml.metadata import ServiceProviderMetadata, UIInfo, NameIDFormat, Service, IdentityProviderMetadata, Subject, \
    Organization
from tests.saml import fixtures
from tests.saml.database_test import DatabaseTest
from tests.test_controller import ControllerTest

SERVICE_PROVIDER_WITH_UNSIGNED_REQUESTS = ServiceProviderMetadata(
    fixtures.SP_ENTITY_ID,
    UIInfo(),
    Organization(),
    NameIDFormat.UNSPECIFIED.value,
    Service(fixtures.SP_ACS_URL, fixtures.SP_ACS_BINDING)
)

SERVICE_PROVIDER_WITH_SIGNED_REQUESTS = ServiceProviderMetadata(
    fixtures.SP_ENTITY_ID,
    UIInfo(),
    Organization(),
    NameIDFormat.UNSPECIFIED.value,
    Service(fixtures.SP_ACS_URL, fixtures.SP_ACS_BINDING),
    True,
    True,
    fixtures.SIGNING_CERTIFICATE,
    fixtures.PRIVATE_KEY
)

IDENTITY_PROVIDERS = [
    IdentityProviderMetadata(
        fixtures.IDP_1_ENTITY_ID,
        UIInfo(),
        Organization(),
        NameIDFormat.UNSPECIFIED.value,
        Service(fixtures.IDP_1_SSO_URL, fixtures.IDP_1_SSO_BINDING),
        signing_certificates=[
            fixtures.SIGNING_CERTIFICATE
        ]
    ),
    IdentityProviderMetadata(
        fixtures.IDP_2_ENTITY_ID,
        UIInfo(),
        Organization(),
        NameIDFormat.UNSPECIFIED.value,
        Service(fixtures.IDP_2_SSO_URL, fixtures.IDP_2_SSO_BINDING)
    )
]


SAML_RESPONSE = \
    '''<?xml version="1.0" encoding="UTF-8"?>
<saml2p:Response Destination="http://opds.hilbertteam.net/SAML2/POST" ID="_fd5cf32afbc789778279262c12d36743" InResponseTo="ONELOGIN_7ad774603b0d8b79fd877628801734d3f6198843" IssueInstant="2020-06-07T23:39:43.836Z" Version="2.0" xmlns:saml2p="urn:oasis:names:tc:SAML:2.0:protocol"><saml2:Issuer xmlns:saml2="urn:oasis:names:tc:SAML:2.0:assertion">http://idp.hilbertteam.net/idp/shibboleth</saml2:Issuer><ds:Signature xmlns:ds="http://www.w3.org/2000/09/xmldsig#"><ds:SignedInfo><ds:CanonicalizationMethod Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#"/><ds:SignatureMethod Algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"/><ds:Reference URI="#_fd5cf32afbc789778279262c12d36743"><ds:Transforms><ds:Transform Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature"/><ds:Transform Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#"/></ds:Transforms><ds:DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"/><ds:DigestValue>lBpSPvuk06OY95wRllXIZnKJesBo4YxSLRXIobKTZ2A=</ds:DigestValue></ds:Reference></ds:SignedInfo><ds:SignatureValue>dRXFZ96XRNR8fqnU7lbJ8LQO401i/HmVNgZJ9VS1/qycbwfYJdAsI0sm9fUw/Dh8NRjiJDaQL1k0MSozQiUtuB8wgnn5oo+F1jr2PipNMERuixVuXVMwxuQL81N8AEgdqSN2/RHNC7fbstE6svAIHaINh5fwldL7IzKhZ1KJr/k=</ds:SignatureValue><ds:KeyInfo><ds:X509Data><ds:X509Certificate>MIICXDCCAcWgAwIBAgIBADANBgkqhkiG9w0BAQ0FADBLMQswCQYDVQQGEwJ1czENMAsGA1UECAwE
T2hpbzETMBEGA1UECgwKQUNNRSwgSW5jLjEYMBYGA1UEAwwPaGlsYmVydHRlYW0ubmV0MB4XDTIw
MDUxODE4MjUyM1oXDTIxMDUxODE4MjUyM1owSzELMAkGA1UEBhMCdXMxDTALBgNVBAgMBE9oaW8x
EzARBgNVBAoMCkFDTUUsIEluYy4xGDAWBgNVBAMMD2hpbGJlcnR0ZWFtLm5ldDCBnzANBgkqhkiG
9w0BAQEFAAOBjQAwgYkCgYEAqx63LDc2vjoYlTvddjPOKDsduR0++A1lmGxdX1N6Ei4NRiWaqBnV
ij6mOqzq5quMA8M3du71aVzE0ELJOuhzrjpu6Rn40KGO6Ewiv3StQkbwAArrrIlIPA9UMpsGD+/o
NXlbF9ZbfqvxEoZcEk6XR6fJT7zXBNzp75dCi39D53MCAwEAAaNQME4wHQYDVR0OBBYEFCmRcv2N
FCSso9IRSFQsdST5FpBfMB8GA1UdIwQYMBaAFCmRcv2NFCSso9IRSFQsdST5FpBfMAwGA1UdEwQF
MAMBAf8wDQYJKoZIhvcNAQENBQADgYEAc/ddQRAswvrlYD8IOA9TCjyqkUJmyJBOj+d0PTzW7lF7
NUyPSp0SunDq12RD8imVq15wNzuzsiIfUZ7F/sp1iFH8ASrBS4sk39stDgUcjFNcwekihUGw3Gfh
GcniFvvia/F82fbPXBPajb9nXNyn3ZwlLsooeC06oIj8FlyHoR8=</ds:X509Certificate></ds:X509Data></ds:KeyInfo></ds:Signature><saml2p:Status><saml2p:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></saml2p:Status><saml2:Assertion ID="_0e6804d477d1c7bc42297ef1447efe50" IssueInstant="2020-06-07T23:39:43.836Z" Version="2.0" xmlns:saml2="urn:oasis:names:tc:SAML:2.0:assertion"><saml2:Issuer>http://idp.hilbertteam.net/idp/shibboleth</saml2:Issuer><ds:Signature xmlns:ds="http://www.w3.org/2000/09/xmldsig#"><ds:SignedInfo><ds:CanonicalizationMethod Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#"/><ds:SignatureMethod Algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"/><ds:Reference URI="#_0e6804d477d1c7bc42297ef1447efe50"><ds:Transforms><ds:Transform Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature"/><ds:Transform Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#"/></ds:Transforms><ds:DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"/><ds:DigestValue>Nd2pbzJsVzYjnKtKv7MB3we+ylbUPbRrOd7LmR0rVP0=</ds:DigestValue></ds:Reference></ds:SignedInfo><ds:SignatureValue>RPM1i8IolvUvQWrgINXO+nRYqW/iIXoOabfPJx7d6AITej0VoK0LzYF5oC4/j/V0amL2ccgGIBiCIF7yXKKycyV0JqrAkfSunAhRK5GkLiApCAQLvcwaWsO3WWh9UA87eUGa9NuCqZ3BbcWmtaCtxJp6d/82fuyzl3tArLi/OqQ=</ds:SignatureValue><ds:KeyInfo><ds:X509Data><ds:X509Certificate>MIICXDCCAcWgAwIBAgIBADANBgkqhkiG9w0BAQ0FADBLMQswCQYDVQQGEwJ1czENMAsGA1UECAwE
T2hpbzETMBEGA1UECgwKQUNNRSwgSW5jLjEYMBYGA1UEAwwPaGlsYmVydHRlYW0ubmV0MB4XDTIw
MDUxODE4MjUyM1oXDTIxMDUxODE4MjUyM1owSzELMAkGA1UEBhMCdXMxDTALBgNVBAgMBE9oaW8x
EzARBgNVBAoMCkFDTUUsIEluYy4xGDAWBgNVBAMMD2hpbGJlcnR0ZWFtLm5ldDCBnzANBgkqhkiG
9w0BAQEFAAOBjQAwgYkCgYEAqx63LDc2vjoYlTvddjPOKDsduR0++A1lmGxdX1N6Ei4NRiWaqBnV
ij6mOqzq5quMA8M3du71aVzE0ELJOuhzrjpu6Rn40KGO6Ewiv3StQkbwAArrrIlIPA9UMpsGD+/o
NXlbF9ZbfqvxEoZcEk6XR6fJT7zXBNzp75dCi39D53MCAwEAAaNQME4wHQYDVR0OBBYEFCmRcv2N
FCSso9IRSFQsdST5FpBfMB8GA1UdIwQYMBaAFCmRcv2NFCSso9IRSFQsdST5FpBfMAwGA1UdEwQF
MAMBAf8wDQYJKoZIhvcNAQENBQADgYEAc/ddQRAswvrlYD8IOA9TCjyqkUJmyJBOj+d0PTzW7lF7
NUyPSp0SunDq12RD8imVq15wNzuzsiIfUZ7F/sp1iFH8ASrBS4sk39stDgUcjFNcwekihUGw3Gfh
GcniFvvia/F82fbPXBPajb9nXNyn3ZwlLsooeC06oIj8FlyHoR8=</ds:X509Certificate></ds:X509Data></ds:KeyInfo></ds:Signature><saml2:Subject><saml2:NameID Format="urn:oasis:names:tc:SAML:2.0:nameid-format:transient" NameQualifier="http://idp.hilbertteam.net/idp/shibboleth" SPNameQualifier="http://opds.hilbertteam.net/metadata/">AAdzZWNyZXQxeAj5TZ2CQ6FkW//TigUE8kgDuJfVEw7mtnCAFq02hvot2hQzlCj5QqQOBRlsAs0dqp1oHoi/apPWmrC2G30BvrtXcDfZsCGQv9eTGSRDydTLVPEe+lfCc1yg3WlxTeiCbFazW6kcybVgUper</saml2:NameID><saml2:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer"><saml2:SubjectConfirmationData Address="185.99.252.212" InResponseTo="ONELOGIN_7ad774603b0d8b79fd877628801734d3f6198843" NotOnOrAfter="2020-06-07T23:44:43.894Z" Recipient="http://opds.hilbertteam.net/SAML2/POST"/></saml2:SubjectConfirmation></saml2:Subject><saml2:Conditions NotBefore="2020-06-07T23:39:43.836Z" NotOnOrAfter="2020-06-07T23:44:43.836Z"><saml2:AudienceRestriction><saml2:Audience>http://opds.hilbertteam.net/metadata/</saml2:Audience></saml2:AudienceRestriction></saml2:Conditions><saml2:AuthnStatement AuthnInstant="2020-06-07T23:39:43.759Z" SessionIndex="_a91f42a8f3d848ee8f3a35912279b93b"><saml2:SubjectLocality Address="185.99.252.212"/><saml2:AuthnContext><saml2:AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport</saml2:AuthnContextClassRef></saml2:AuthnContext></saml2:AuthnStatement><saml2:AttributeStatement><saml2:Attribute FriendlyName="uid" Name="urn:oid:0.9.2342.19200300.100.1.1" NameFormat="urn:oasis:names:tc:SAML:2.0:attrname-format:uri"><saml2:AttributeValue>student1</saml2:AttributeValue></saml2:Attribute><saml2:Attribute FriendlyName="mail" Name="urn:oid:0.9.2342.19200300.100.1.3" NameFormat="urn:oasis:names:tc:SAML:2.0:attrname-format:uri"><saml2:AttributeValue>student1@idptestbed.edu</saml2:AttributeValue></saml2:Attribute><saml2:Attribute FriendlyName="sn" Name="urn:oid:2.5.4.4" NameFormat="urn:oasis:names:tc:SAML:2.0:attrname-format:uri"><saml2:AttributeValue>Ent</saml2:AttributeValue></saml2:Attribute><saml2:Attribute FriendlyName="givenName" Name="urn:oid:2.5.4.42" NameFormat="urn:oasis:names:tc:SAML:2.0:attrname-format:uri"><saml2:AttributeValue>Stud</saml2:AttributeValue></saml2:Attribute></saml2:AttributeStatement></saml2:Assertion></saml2p:Response>'''


class SAMLAuthenticationManagerTest(ControllerTest):
    @parameterized.expand([
        ('with_unsigned_authentication_request', SERVICE_PROVIDER_WITH_UNSIGNED_REQUESTS, IDENTITY_PROVIDERS),
        ('with_signed_authentication_request', SERVICE_PROVIDER_WITH_SIGNED_REQUESTS, IDENTITY_PROVIDERS)
    ])
    def test_start_authentication(self, name, service_provider, identity_providers):
        configuration = create_autospec(spec=SAMLConfiguration)
        configuration.get_debug = MagicMock(return_value=False)
        configuration.get_strict = MagicMock(return_value=False)
        configuration.get_service_provider = MagicMock(return_value=service_provider)
        configuration.get_identity_providers = MagicMock(return_value=identity_providers)
        onelogin_configuration = SAMLOneLoginConfiguration(configuration)
        authentication_manager = SAMLAuthenticationManager(onelogin_configuration)

        with self.app.test_request_context('/'):
            result = authentication_manager.start_authentication(self._db, fixtures.IDP_1_ENTITY_ID, '')

            query_items = urlparse.parse_qs(urlparse.urlsplit(result).query)
            saml_request = query_items['SAMLRequest'][0]
            decoded_saml_request = OneLogin_Saml2_Utils.decode_base64_and_inflate(saml_request)

            validation_result = OneLogin_Saml2_Utils.validate_xml(
                decoded_saml_request,
                'saml-schema-protocol-2.0.xsd',
                False
            )
            assert isinstance(validation_result, Document)

            saml_request_dom = fromstring(decoded_saml_request)

            acs_url = saml_request_dom.get('AssertionConsumerServiceURL')
            eq_(acs_url, SERVICE_PROVIDER_WITH_UNSIGNED_REQUESTS.acs_service.url)

            acs_binding = saml_request_dom.get('ProtocolBinding')
            eq_(acs_binding, SERVICE_PROVIDER_WITH_UNSIGNED_REQUESTS.acs_service.binding.value)

            sso_url = saml_request_dom.get('Destination')
            eq_(sso_url, IDENTITY_PROVIDERS[0].sso_service.url)

            name_id_policy_nodes = OneLogin_Saml2_Utils.query(saml_request_dom, './samlp:NameIDPolicy')

            assert name_id_policy_nodes is not None
            eq_(len(name_id_policy_nodes), 1)

            name_id_policy_node = name_id_policy_nodes[0]
            name_id_format = name_id_policy_node.get('Format')

            eq_(name_id_format, SERVICE_PROVIDER_WITH_UNSIGNED_REQUESTS.name_id_format)

    def test_finish_authentication(self):
        configuration = create_autospec(spec=SAMLConfiguration)
        configuration.get_debug = MagicMock(return_value=False)
        configuration.get_strict = MagicMock(return_value=False)
        configuration.get_service_provider = MagicMock(return_value=SERVICE_PROVIDER_WITH_UNSIGNED_REQUESTS)
        configuration.get_identity_providers = MagicMock(return_value=IDENTITY_PROVIDERS)
        onelogin_configuration = SAMLOneLoginConfiguration(configuration)
        authentication_manager = SAMLAuthenticationManager(onelogin_configuration)

        saml_response = b64encode(SAML_RESPONSE)
        with self.app.test_request_context('/', data={
            'SAMLResponse': saml_response
        }):
            result = authentication_manager.finish_authentication(self._db, fixtures.IDP_1_ENTITY_ID)

            assert isinstance(result, Subject)


class SAMLAuthenticationManagerFactoryTest(DatabaseTest):
    def test_create(self):
        # Arrange
        factory = SAMLAuthenticationManagerFactory()
        integration_owner = create_autospec(spec=ExternalIntegrationOwner)
        integration_owner.external_integration = MagicMock(return_value=self._integration)

        # Act
        result = factory.create(integration_owner)

        # Assert
        assert isinstance(result, SAMLAuthenticationManager)