# BotoClientUtils
Boto3 is written for single threaded applications. While it may be OK for most applications and scripts, it is a problem for applications/scripts that performs operations in multiple AWS accounts and/or regions. In this case, a single threaded application/script will take ages to complete its tasks and a multi-threaded scripts will typically require loads of client/resource instantiation with underlying AssumeRole operations, thus wasting time again.


The module is aimed at simplify the management of multiple boto clients/resources in the context of multithreaded Python programs.
It is also oriented toward situations where there will be the need of creating multiple clients using roles assumed in multiple different accounts.

## SessionManager
This class produce objects that will manage boto3 clients/resources.
### SessionManager instanciation parameters
  - `remote_role_name` : MANDATORY - the name of the IAM role that the SessionManager will assume when asked for a client/resource out of the current account. Can be None but you will not be able to create client/resource for other accounts.
  - `role_session_name` : OPTIONAL - the name of the session when sts:AssumeRole is called. Defaults to `default`
#### Exemple
```python
from botoclientutils import SessionManager
sm = SessionManager('my-remote-role-existing-in-all-my-accounts', 'my-session-name')
```

### Get client/resource
####  Behavior
When asked for a client or resource, the SessionManager object will behave as follow (it is a little more complicated than that actually):
- If the client already exists for this account/region/thread:
  - If it is not close to expire => return the existing client
  - If it is close to expire => renew the underlying session, create a new client and return it
- If the client does not exists for this account/region/thread:
  - If a valid session exists for this account/region => create a new client and return it
  - If a valid session does not exist for this account/region => create the underlying session, create a new client and return it

In short, SessionManager will minimize the number of AssumeRole and client/resource association needed while ensuring that there is always different client/resource for different threads.
> **Note:** The `name` of the thread is used for identification, **not** its number. If left to itself, new threads will always have new names and consequently new client/resource will always be created. On the other hand, managing the thread name cleverly allow one to reuse existing clients even in the case where the threads are short lived.

####  Parameters
  - `name` : MANDATORY - the name of the client/resource. It will be passed to the underlying boto3 instanciation function
  - `account` : OPTIONAL - the AWS Account ID in which the client/resource will operate. The SessionManager will assume the role for which it has been instanciated. Defaults to the current account (no role assumed).
  - `region` : OPTIONAL -  the AWS region in which the client/resource will operate. Defaults to `eu-west-1`

#### Exemple
```python
from botoclientutils import SessionManager
import threading

sm = SessionManager('a-role-existing-in-all-my-accounts')

def list_instances(account, region, result_list):
    instances = sm.client('ec2', account, region).describe_instances()
    result_list.extend([i for r in instances['Reservations'] for i in r['Instances']])

accounts = ['123456789012', '234567890123', '345678901234']
regions = ['eu-west-1', 'eu-west-3']

# List all the EC2 instances of all my accounts in no time
all_my_instances = list()
for a in accounts:
    for r in regions:
        threading.Thread(target=list_instances, args=(a, r, all_my_instances)).start()
for t in [t for t in threading.enumerate() if t is not threading.current_thread()]:
    t.join()

print(all_my_instances)
```

## SessionManagerFactory
This object is a Singleton and a callable. It allows to retrieve SessionManager objects in all your modules and files without worrying about uselessly duplicating them.
The object is called with the same arguments as the SessionManager constructor. It will check if a SessionManager for those arguments already exists and if yes return it. If no it will create a new one.
#### Exemple
```python
from botoclientutils import SessionManagerFactory

sm = SessionManagerFactory()('a-role-existing-in-all-my-accounts')
```
