# Local service lifecycle

The service binds to loopback only and exposes the local engine toolbox. It has no permission modes. Engine writes are never automatically replayed. Disabling this package stops its owned child service; it does not remove the public Host from the engine.
