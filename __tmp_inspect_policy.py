from openhands.sdk.confirmation import policies
import inspect
print([name for name in dir(policies) if 'Confirm' in name or 'Approval' in name or 'Policy' in name])
print('---')
for name in [n for n in dir(policies) if 'Confirm' in n or 'Approval' in n or 'Policy' in n]:
    obj=getattr(policies,name)
    if inspect.isclass(obj):
        try:
            print(name, obj.__mro__)
            sig=inspect.signature(obj)
            print(sig)
        except Exception:
            pass
