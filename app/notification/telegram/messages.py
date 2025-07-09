# In this file, we define message templates for Telegram notifications.
# Using templates helps to avoid string concatenation and improves code readability.

USER_STATUS_CHANGE = """
{status}
➖➖➖➖➖➖➖➖➖
**Username:** `{username}`
➖➖➖➖➖➖➖➖➖
_Belongs To_: `{admin_username}`
_By: #{by}_
"""

CREATE_USER = """
🆕 #Create_User
➖➖➖➖➖➖➖➖➖
**Username:** `{username}`
**Data Limit**: `{data_limit}`
**Expire Date:** `{expire_date}`
**Data Limit Reset Strategy:** `{data_limit_reset_strategy}`
**Has Next Plan**: `{has_next_plan}`
➖➖➖➖➖➖➖➖➖
_Belongs To_: `{admin_username}`
_By: #{by}_
"""

MODIFY_USER = """
✏️ #Modify_User
➖➖➖➖➖➖➖➖➖
**Username:** `{username}`
**Data Limit**: `{data_limit}`
**Expire Date:** `{expire_date}`
**Data Limit Reset Strategy:** `{data_limit_reset_strategy}`
**Has Next Plan**: `{has_next_plan}`
➖➖➖➖➖➖➖➖➖
_Belongs To_: `{admin_username}`
_By: #{by}_
"""

REMOVE_USER = """
🗑️ #Remove_User
➖➖➖➖➖➖➖➖➖
**Username:** `{username}`
➖➖➖➖➖➖➖➖➖
_Belongs To_: `{admin_username}`
_By: #{by}_
"""

RESET_USER_DATA_USAGE = """
🔁 #Reset_User_Data_Usage
➖➖➖➖➖➖➖➖➖
**Username:** `{username}`
**Data Limit**: `{data_limit}`
➖➖➖➖➖➖➖➖➖
_Belongs To_: `{admin_username}`
_By: #{by}_
"""

USER_DATA_RESET_BY_NEXT = """
🔁 #Reset_User_By_Next
➖➖➖➖➖➖➖➖➖
**Username:** `{username}`
**Data Limit**: `{data_limit}`
**Expire Date:** `{expire_date}`
➖➖➖➖➖➖➖➖➖
_Belongs To_: `{admin_username}`
_By: #{by}_
"""

USER_SUBSCRIPTION_REVOKED = """
🛑 #Revoke_User_Subscribtion
➖➖➖➖➖➖➖➖➖
**Username:** `{username}`
➖➖➖➖➖➖➖➖➖
_Belongs To_: `{admin_username}`
_By: #{by}_
"""

CREATE_ADMIN = """
#Create_Admin
➖➖➖➖➖➖➖➖➖
**Username:** `{username}`
**Is Sudo:** `{is_sudo}`
**Is Disabled:** `{is_disabled}`
**Used Traffic:** `{used_traffic}`
➖➖➖➖➖➖➖➖➖
_By: #{by}_
"""

MODIFY_ADMIN = """
#Modify_Admin
➖➖➖➖➖➖➖➖➖
**Username:** `{username}`
**Is Sudo:** `{is_sudo}`
**Is Disabled:** {is_disabled}
**Used Traffic:** {used_traffic}
➖➖➖➖➖➖➖➖➖
_By: #{by}_
"""

REMOVE_ADMIN = """
#Remove_Admin
**Username:** `{username}`
➖➖➖➖➖➖➖➖➖
_By: #{by}_
"""

ADMIN_RESET_USAGE = """
#Admin_Usage_Reset
**Username:** `{username}`
➖➖➖➖➖➖➖➖➖
_By: #{by}_
"""

ADMIN_LOGIN = """
#Login_Attempt
*Status*: {status}
➖➖➖➖➖➖➖➖➖
**Username:** `{username}`
**Password:** {password}
**IP:** `{client_ip}`
"""

CREATE_HOST = """
#Create_Host
➖➖➖➖➖➖➖➖➖
**Remark:** `{remark}`
**Address:** `{address}`
**Inbound Tag:** `{tag}`
**Port:** `{port}`
➖➖➖➖➖➖➖➖➖
_ID_: `{id}`
_By: #{by}_
"""

MODIFY_HOST = """
#Modify_Host
➖➖➖➖➖➖➖➖➖
**Remark:** `{remark}`
**Address:** `{address}`
**Inbound Tag:** `{tag}`
**Port:** `{port}`
➖➖➖➖➖➖➖➖➖
_ID_: `{id}`
_By: #{by}_
"""

REMOVE_HOST = """
#Remove_Host
➖➖➖➖➖➖➖➖➖
**Remark:** `{remark}`
➖➖➖➖➖➖➖➖➖
_ID_: {id}
_By: #{by}_
"""

MODIFY_HOSTS = """
#Modify_Hosts
➖➖➖➖➖➖➖➖➖
All hosts has been updated by **#{by}**
"""

CREATE_NODE = """
#Create_Node
➖➖➖➖➖➖➖➖➖
**ID:** `{id}`
**Name:** `{name}`
**Address:** `{address}`
**Port:** `{port}`
➖➖➖➖➖➖➖➖➖
_By: #{by}_
"""

MODIFY_NODE = """
#Modify_Node
➖➖➖➖➖➖➖➖➖
**ID:** `{id}`
**Name:** `{name}`
**Address:** `{address}`
**Port:** `{port}`
➖➖➖➖➖➖➖➖➖
_By: #{by}_
"""

REMOVE_NODE = """
#Remove_Node
➖➖➖➖➖➖➖➖➖
**ID:** `{id}`
**Name:** `{name}`
➖➖➖➖➖➖➖➖➖
_By: #{by}_
"""

CONNECT_NODE = """
#Connect_Node
➖➖➖➖➖➖➖➖➖
**Name:** `{name}`
**Node Version:** {node_version}
**Core Version:** {core_version}
➖➖➖➖➖➖➖➖➖
_ID_: `{id}`
"""

ERROR_NODE = """
#Error_Node
➖➖➖➖➖➖➖➖➖
**Name:** `{name}`
**Error:** {error}
➖➖➖➖➖➖➖➖➖
_ID_: `{id}`
"""

CREATE_USER_TEMPLATE = """
#Create_User_Template
➖➖➖➖➖➖➖➖➖
**Name:** `{name}`
**Data Limit:** `{data_limit}`
**Expire Duration:** `{expire_duration}`
**Username Prefix:** `{username_prefix}`
**Username Suffix:** `{username_suffix}`
➖➖➖➖➖➖➖➖➖
_By: #{by}_
"""

MODIFY_USER_TEMPLATE = """
#Modify_User_Template
➖➖➖➖➖➖➖➖➖
**Name:** `{name}`
**Data Limit:** `{data_limit}`
**Expire Duration:** `{expire_duration}`
**Username Prefix:** `{username_prefix}`
**Username Suffix:** `{username_suffix}`
➖➖➖➖➖➖➖➖➖
_By: #{by}_
"""

REMOVE_USER_TEMPLATE = """
#Remove_User_Template
**Name:** `{name}`
➖➖➖➖➖➖➖➖➖
_By: #{by}_
"""

CREATE_CORE = """
#Create_core
➖➖➖➖➖➖➖➖➖
**Name:** `{name}`
**Exclude inbound tags:** `{exclude_inbound_tags}`
**Fallbacks inbound tags:** `{fallbacks_inbound_tags}`
➖➖➖➖➖➖➖➖➖
_ID_: `{id}`
_By: #{by}_
"""

MODIFY_CORE = """
#Modify_core
➖➖➖➖➖➖➖➖➖
**Name:** `{name}`
**Exclude inbound tags:** `{exclude_inbound_tags}`
**Fallbacks inbound tags:** `{fallbacks_inbound_tags}`
➖➖➖➖➖➖➖➖➖
_ID_: `{id}`
_By: #{by}_
"""

REMOVE_CORE = """
#Remove_core
➖➖➖➖➖➖➖➖➖
**ID:** `{id}`
➖➖➖➖➖➖➖➖➖
_By: #{by}_
"""

CREATE_GROUP = """
#Create_Group
➖➖➖➖➖➖➖➖➖
**Name:** `{name}`
**Inbound Tags:** `{inbound_tags}`
**Is Disabled:** `{is_disabled}`
➖➖➖➖➖➖➖➖➖
_ID_: `{id}`
_By: #{by}_
"""

MODIFY_GROUP = """
#Modify_Group
➖➖➖➖➖➖➖➖➖
**Name:** `{name}`
**Inbound Tags:** `{inbound_tags}`
**Is Disabled:** `{is_disabled}`
➖➖➖➖➖➖➖➖➖
_ID_: `{id}`
_By: #{by}_
"""

REMOVE_GROUP = """
#Remove_Group
➖➖➖➖➖➖➖➖➖
**ID:** `{id}`
➖➖➖➖➖➖➖➖➖
_By: #{by}_
"""
