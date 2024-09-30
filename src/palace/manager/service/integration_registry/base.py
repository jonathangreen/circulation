from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from typing import Generic, Literal, TypeVar, cast, overload

from palace.manager.core.exceptions import BasePalaceException
from palace.manager.integration.goals import Goals

T = TypeVar("T", covariant=True)
V = TypeVar("V")


class RegistrationException(BasePalaceException, ValueError):
    """An error occurred while registering an integration."""


class LookupException(BasePalaceException, LookupError):
    """An error occurred while looking up an integration."""


class IntegrationRegistry(Generic[T]):
    def __init__(self, goal: Goals, integrations: dict[str, type[T]] | None = None):
        """Initialize a new IntegrationRegistry."""
        self._lookup: dict[str, type[T]] = {}
        self._reverse_lookup: dict[type[T], list[str]] = defaultdict(list)
        self.goal = goal

        if integrations:
            for protocol, integration in integrations.items():
                self.register(integration, canonical=protocol)

    def register(
        self,
        integration: type[T],
        *,
        canonical: str | None = None,
        aliases: list[str] | None = None,
    ) -> type[T]:
        """
        Register an integration class.

        If no canonical protocol name is provided, the integration class's
        name will be used, otherwise the class name will be added as an alias.

        Aliases are additional names that can be used to look up the integration
        class.
        """

        if aliases is None:
            aliases = []

        if canonical is None:
            canonical = integration.__name__
        else:
            aliases.append(integration.__name__)

        for protocol in [canonical] + aliases:
            if protocol in self._lookup and self._lookup[protocol] != integration:
                raise RegistrationException(
                    f"Integration {protocol} already registered"
                )
            self._lookup[protocol] = integration
            self._reverse_lookup[integration].append(protocol)

        return integration

    @overload
    def get(self, protocol: str, default: None = ...) -> type[T] | None: ...

    @overload
    def get(self, protocol: str, default: V) -> type[T] | V: ...

    def get(self, protocol: str, default: V | None = None) -> type[T] | V | None:
        """Look up an integration class by protocol."""
        if protocol not in self._lookup:
            return default
        return self[protocol]

    @overload
    def get_protocol(self, integration: type[T], default: None = ...) -> str | None: ...

    @overload
    def get_protocol(self, integration: type[T], default: Literal[False]) -> str: ...

    @overload
    def get_protocol(self, integration: type[T], default: V) -> str | V: ...

    def get_protocol(
        self, integration: type[T], default: V | None | Literal[False] = None
    ) -> str | V | None:
        """Look up the canonical protocol for an integration class."""
        names = self.get_protocols(integration, default)
        # We have to cast here because mypy doesn't understand that
        # if default is False, names is a list[str] due to the overload
        # for get_protocols.
        if names is default:
            return cast(V | None, names)
        return cast(list[str], names)[0]

    @overload
    def get_protocols(
        self, integration: type[T], default: None = ...
    ) -> list[str] | None: ...

    @overload
    def get_protocols(
        self, integration: type[T], default: Literal[False]
    ) -> list[str]: ...

    @overload
    def get_protocols(self, integration: type[T], default: V) -> list[str] | V: ...

    def get_protocols(
        self, integration: type[T], default: V | None | Literal[False] = None
    ) -> list[str] | V | None:
        """Look up all protocols for an integration class."""
        if integration not in self._reverse_lookup:
            if default is False:
                raise LookupException(f"Integration {integration} not found")
            return default
        return self._reverse_lookup[integration]

    @property
    def integrations(self) -> set[type[T]]:
        """Return a set of all registered canonical protocols."""
        return set(self._reverse_lookup.keys())

    def update(self, other: IntegrationRegistry[T]) -> None:
        """Update registry to include integrations in other."""
        if self.goal != other.goal:
            raise RegistrationException(
                f"IntegrationRegistry's goals must be the same. (Self: {self.goal}, Other: {other.goal})"
            )

        for integration in other.integrations:
            names = other.get_protocols(integration)
            assert isinstance(names, list)
            self.register(integration, canonical=names[0], aliases=names[1:])

    def canonicalize(self, protocol: str) -> str:
        """Return the canonical protocol name for a given protocol."""
        return self.get_protocol(self[protocol], default=False)

    def equivalent(self, protocol1: str | None, protocol2: str | None) -> bool:
        """Return whether two protocols are equivalent."""
        if (
            protocol1 is None
            or protocol1 not in self
            or protocol2 is None
            or protocol2 not in self
        ):
            return False

        return self[protocol1] is self[protocol2]

    def __iter__(self) -> Iterator[tuple[str, type[T]]]:
        for integration, names in self._reverse_lookup.items():
            yield names[0], integration

    def __getitem__(self, protocol: str) -> type[T]:
        """Look up an integration class by protocol, using the [] operator."""
        try:
            return self._lookup[protocol]
        except KeyError as e:
            raise LookupException(f"Integration {protocol} not found") from e

    def __len__(self) -> int:
        """Return the number of registered integration classes."""
        return len(self._reverse_lookup)

    def __contains__(self, name: str) -> bool:
        """Return whether an integration class is registered under the given name."""
        return name in self._lookup

    def __repr__(self) -> str:
        return f"<IntegrationRegistry: {self._lookup}>"

    def __add__(self, other: IntegrationRegistry[V]) -> IntegrationRegistry[T | V]:
        if not isinstance(other, IntegrationRegistry):
            raise TypeError(
                f"unsupported operand type(s) for +: 'IntegrationRegistry' and '{type(other).__name__}'"
            )

        new: IntegrationRegistry[T | V] = IntegrationRegistry(self.goal)
        new.update(self)
        new.update(other)
        return new
