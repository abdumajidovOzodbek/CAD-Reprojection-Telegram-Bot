# Admin Commands for CAD Reprojection Bot

## Your Admin ID
- **Main Admin**: 7657310374 (you)

## Pre-Authorized Users
- 7657310374 (Main Admin)
- 26181418

---

## Commands (Admin Only)

### `/start` or `/help`
Shows the welcome message and bot instructions.

### `/users`
Lists all authorized users with their names, usernames, and User IDs.

**Example:**
```
/users
```

**Response:**
```
👥 Authorized Users (2):

• John Doe (@johndoe)
  ID: 123456789

• Jane Smith
  ID: 987654321
```

The display shows:
- First name and last name (if available)
- Username with @ symbol (if they have one)
- User ID for use with `/revoke` command

---

### `/revoke <user_id>`
Revokes access for a specific user. They will be immediately blocked from using the bot.

**Example:**
```
/revoke 26181418
```

**Response:**
```
🚫 User ID 26181418's access has been revoked.
```

The user will also receive a notification that their access has been revoked.

---

## Approval Workflow

### When a New User Tries to Use the Bot:

1. **User sends** `/start` or uploads a file
2. **You receive** a message with detailed user info and buttons:
   ```
   🔔 New user access request:
   
   👤 Name: John Doe
   🆔 User ID: 123456789
   📝 Username: @johndoe
   
   Do you want to allow this user?
   
   [✅ Allow]  [❌ Deny]
   ```
   
   This shows:
   - Full name (first + last name if available)
   - Telegram User ID
   - Username (with @ if they have one, or "No username")

3. **Click ✅ Allow** to approve
   - User info is stored (name, username, ID)
   - User gets added to authorized list
   - User receives approval notification
   - User can now use the bot

4. **Click ❌ Deny** to reject
   - User receives denial notification
   - User cannot use the bot

---

## How to Revoke Access After Approval

### Option 1: Use /revoke command
```
/revoke 123456789
```

### Option 2: Check users first, then revoke
```
/users
```
Then copy the user ID and:
```
/revoke <copied_user_id>
```

---

## Important Notes

- **User information is tracked**: The bot stores first name, last name, and username for all users
- **Info updates automatically**: When authorized users interact with the bot, their info is refreshed
- **You cannot revoke your own access** (Main Admin: 7657310374)
- **Revoked users are notified** automatically
- **No one else can approve/deny** requests - only the main admin (you)
- **Authorization persists** until you revoke it (even after bot restarts)
- **Use `/users` to see** who everyone is before revoking

---

## User Experience

### For Authorized Users:
- Send DXF or DWG files
- Bot processes and returns reprojected Shapefile ZIP

### For Unauthorized Users:
- Get message: "You don't have access to this bot yet. An authorization request has been sent to the admin. Please wait for approval."
- Cannot upload files until approved

### For Revoked Users:
- Get message: "You don't have access to this bot. Send /start to request access."
- Must request access again (you'll get a new approval request)
